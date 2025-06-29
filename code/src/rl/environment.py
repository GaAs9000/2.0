import torch
import numpy as np
from typing import Dict, Tuple, List, Optional, Union, Any
from torch_geometric.data import HeteroData
import copy

try:
    from .state import StateManager
    from .action_space import ActionSpace, ActionMask
    from .reward import RewardFunction
    from .utils import MetisInitializer, PartitionEvaluator
except ImportError:
    # 如果相对导入失败，尝试绝对导入
    try:
        from rl.state import StateManager
        from rl.action_space import ActionSpace, ActionMask
        from rl.reward import RewardFunction
        from rl.utils import MetisInitializer, PartitionEvaluator
    except ImportError:
        print("警告：无法导入RL模块的某些组件")


class PowerGridPartitioningEnv:
    """
    电力网络分割MDP环境
    """

    def __init__(self,
                 hetero_data: HeteroData,
                 node_embeddings: Dict[str, torch.Tensor],
                 num_partitions: int,
                 reward_weights: Dict[str, float] = None,
                 max_steps: int = 200,
                 device: torch.device = None,
                 attention_weights: Dict[str, torch.Tensor] = None,
                 config: Dict[str, Any] = None):
        """
        初始化电力网络分割环境

        参数:
            hetero_data: 来自数据处理的异构图数据
            node_embeddings: GAT编码器预计算的节点嵌入
            num_partitions: 目标分区数量（K）
            reward_weights: 奖励组件权重
            max_steps: 每个回合的最大步数
            device: 用于计算的Torch设备
            attention_weights: GAT编码器注意力权重，用于增强嵌入
            config: 配置字典，用于控制输出详细程度
        """
        self.device = device or torch.device('cpu')
        self.hetero_data = hetero_data.to(self.device)
        self.num_partitions = num_partitions
        self.max_steps = max_steps
        self.config = config

        # 生成增强的节点嵌入（如果提供了注意力权重）
        enhanced_embeddings = self._generate_enhanced_embeddings(
            node_embeddings, attention_weights
        ) if attention_weights else node_embeddings

        # 初始化核心组件
        self.state_manager = StateManager(hetero_data, enhanced_embeddings, device, config)
        self.action_space = ActionSpace(hetero_data, num_partitions, device)

        # 【新增】奖励函数支持
        # 支持三种奖励模式：legacy（原有）、enhanced（增强）、dual_layer（双层）
        self.reward_mode = reward_weights.get('reward_mode', 'legacy') if reward_weights else 'legacy'

        if self.reward_mode == 'dual_layer':
            # 使用新的双层奖励函数
            from .reward import DualLayerRewardFunction
            self.reward_function = DualLayerRewardFunction(
                hetero_data,
                config={'reward_weights': reward_weights, 'thresholds': reward_weights.get('thresholds', {})},
                device=device
            )
        elif self.reward_mode == 'enhanced':
            # 使用增强奖励函数
            from .reward import EnhancedRewardFunction
            enhanced_config = reward_weights.get('enhanced_config', {})
            self.reward_function = EnhancedRewardFunction(
                hetero_data,
                reward_weights,
                device,
                **enhanced_config
            )
        else:
            # 保持原有的增量奖励机制
            self.reward_function = None

        self.metis_initializer = MetisInitializer(hetero_data, device, config)
        self.evaluator = PartitionEvaluator(hetero_data, device)

        # 【保留】用于存储上一步的指标，实现增量奖励
        self.previous_metrics = None
        
        # 环境状态
        self.current_step = 0
        self.episode_history = []
        self.is_terminated = False
        self.is_truncated = False
        
        # 缓存频繁使用的数据
        self._setup_cached_data()
        
    def _setup_cached_data(self):
        """设置频繁访问的缓存数据"""
        # 所有类型节点的总数
        self.total_nodes = sum(x.shape[0] for x in self.hetero_data.x_dict.values())
        
        # 全局节点映射（本地索引到全局索引）
        self.global_node_mapping = self.state_manager.get_global_node_mapping()
        
        # 用于奖励计算的边信息
        self.edge_info = self._extract_edge_info()
        
    def _extract_edge_info(self) -> Dict[str, torch.Tensor]:
        """提取奖励计算所需的边信息"""
        edge_info = {}
        
        # 收集所有边及其属性
        all_edges = []
        all_edge_attrs = []
        
        for edge_type, edge_index in self.hetero_data.edge_index_dict.items():
            edge_attr = self.hetero_data.edge_attr_dict[edge_type]
            
            # 将本地索引转换为全局索引
            src_type, _, dst_type = edge_type
            src_global = self.state_manager.local_to_global(edge_index[0], src_type)
            dst_global = self.state_manager.local_to_global(edge_index[1], dst_type)
            
            global_edges = torch.stack([src_global, dst_global], dim=0)
            all_edges.append(global_edges)
            all_edge_attrs.append(edge_attr)
        
        edge_info['edge_index'] = torch.cat(all_edges, dim=1)
        edge_info['edge_attr'] = torch.cat(all_edge_attrs, dim=0)
        
        return edge_info

    def _generate_enhanced_embeddings(self,
                                    node_embeddings: Dict[str, torch.Tensor],
                                    attention_weights: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        """
        生成增强的静态节点特征嵌入 H'

        将边级注意力权重聚合为节点特征，然后与原始嵌入连接

        参数:
            node_embeddings: 原始节点嵌入 H
            attention_weights: GAT编码器的边级注意力权重

        返回:
            enhanced_embeddings: 增强的节点嵌入 H' = concat(H, H_attn)
        """
        # 步骤1：计算每个节点的聚合注意力分数
        node_attention_scores = self._aggregate_attention_to_nodes(attention_weights)

        # 步骤2：将注意力分数与原始嵌入连接
        enhanced_embeddings = {}

        for node_type, embeddings in node_embeddings.items():
            # 获取该节点类型的注意力分数
            if node_type in node_attention_scores and node_attention_scores[node_type] is not None:
                attention_features = node_attention_scores[node_type]

                # 检查数值稳定性
                if torch.isnan(embeddings).any() or torch.isinf(embeddings).any():
                    print(f"  ⚠️ {node_type}: 检测到原始嵌入中的NaN/Inf值")
                    embeddings = torch.nan_to_num(embeddings, nan=0.0, posinf=1.0, neginf=-1.0)

                if torch.isnan(attention_features).any() or torch.isinf(attention_features).any():
                    print(f"  ⚠️ {node_type}: 检测到注意力特征中的NaN/Inf值")
                    attention_features = torch.nan_to_num(attention_features, nan=0.0, posinf=1.0, neginf=-1.0)

                # 连接原始嵌入和注意力特征: H' = concat(H, H_attn)
                enhanced_emb = torch.cat([embeddings, attention_features], dim=1)

                # 检查连接后的嵌入是否有极值
                if torch.isnan(enhanced_emb).any() or torch.isinf(enhanced_emb).any():
                    print(f"  ⚠️ {node_type}: 检测到增强嵌入中的NaN/Inf值，进行清理")
                    enhanced_emb = torch.nan_to_num(enhanced_emb, nan=0.0, posinf=1.0, neginf=-1.0)

                enhanced_embeddings[node_type] = enhanced_emb
            else:
                # 如果没有注意力权重，使用原始嵌入
                # 仍然检查数值稳定性
                if torch.isnan(embeddings).any() or torch.isinf(embeddings).any():
                    print(f"  ⚠️ {node_type}: 检测到原始嵌入中的NaN/Inf值")
                    embeddings = torch.nan_to_num(embeddings, nan=0.0, posinf=1.0, neginf=-1.0)

                enhanced_embeddings[node_type] = embeddings

        return enhanced_embeddings

    def _aggregate_attention_to_nodes(self,
                                    attention_weights: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        """
        将边级注意力权重聚合为节点级特征

        对每个节点 i，计算其聚合注意力分数：
        h_attn(i) = (1/|N(i)|) * Σ_{j ∈ N(i)} α_{j→i}

        参数:
            attention_weights: 边级注意力权重字典

        返回:
            node_attention_scores: 每个节点类型的注意力分数 [num_nodes, 1]
        """
        # 初始化节点注意力分数累积器
        node_attention_accumulator = {}
        node_degree_counter = {}
        edge_type_to_key_mapping = {}  # 添加这个变量

        # 初始化所有节点类型的累积器
        for node_type in self.hetero_data.x_dict.keys():
            num_nodes = self.hetero_data.x_dict[node_type].shape[0]
            node_attention_accumulator[node_type] = torch.zeros(num_nodes, device=self.device)
            node_degree_counter[node_type] = torch.zeros(num_nodes, device=self.device)

        # 处理每种边类型
        has_attention = False
        for edge_type, edge_index in self.hetero_data.edge_index_dict.items():
            src_type, relation, dst_type = edge_type
            
            # 使用改进的键匹配，找不到时返回None
            attn_weights = self._get_attention_weights_for_edge_type(
                edge_type, attention_weights, edge_type_to_key_mapping
            )
            
            # 如果找不到权重，则跳过此边类型
            if attn_weights is None:
                continue
            
            has_attention = True
            # 处理维度和多头注意力（维度不匹配时返回None）
            processed_weights = self._process_attention_weights(
                attn_weights, edge_index, edge_type
            )
            
            # 如果权重处理失败（如维度不匹配），则跳过此边类型
            if processed_weights is None:
                continue
            
            # 高效的节点聚合
            dst_nodes = edge_index[1]
            node_attention_accumulator[dst_type].index_add_(0, dst_nodes, processed_weights)
            node_degree_counter[dst_type].index_add_(0, dst_nodes, torch.ones_like(processed_weights))

        # 如果没有任何注意力权重，直接返回
        if not has_attention:
            return {node_type: None for node_type in self.hetero_data.x_dict.keys()}

        # 计算平均注意力分数（避免除零）
        node_attention_scores = {}
        for node_type in node_attention_accumulator.keys():
            degrees = node_degree_counter[node_type]
            # 避免除零：度为0的节点注意力分数设为0
            avg_attention = torch.where(
                degrees > 0,
                node_attention_accumulator[node_type] / degrees,
                torch.zeros_like(node_attention_accumulator[node_type])
            )

            # 转换为列向量 [num_nodes, 1]
            node_attention_scores[node_type] = avg_attention.unsqueeze(1)

        return node_attention_scores

    def _get_attention_weights_for_edge_type(self,
                                            edge_type: tuple,
                                            attention_weights: Dict[str, torch.Tensor],
                                            edge_type_to_key_mapping: Dict[str, str]) -> Optional[torch.Tensor]:
        """
        获取特定边类型的注意力权重

        参数:
            edge_type: 边类型 (src_type, relation, dst_type)
            attention_weights: 边级注意力权重字典
            edge_type_to_key_mapping: 边类型到注意力权重键的映射

        返回:
            找到的注意力权重，如果找不到则返回None
        """
        # 构建边类型到注意力权重键的映射
        edge_type_key = f"{edge_type[0]}__{edge_type[1]}__{edge_type[2]}"
        edge_type_to_key_mapping[edge_type_key] = edge_type_key

        # 尝试多种键格式来查找注意力权重
        found_weights = None
        used_key = None

        # 1. 尝试标准格式
        if edge_type_key in attention_weights:
            found_weights = attention_weights[edge_type_key]
            used_key = edge_type_key
        else:
            # 2. 尝试查找包含相关信息的键
            for key, weights in attention_weights.items():
                if (edge_type[0] in key and edge_type[2] in key and edge_type[1] in key) or \
                   ("unknown_edge_type" in key):
                    found_weights = weights
                    used_key = key
                    break

        if found_weights is None:
            # print(f"    ⚠️ 未找到边类型 {edge_type_key} 的注意力权重")
            # print(f"       可用的注意力权重键: {list(attention_weights.keys())}")
            return None

        attn_weights = found_weights.to(self.device)
        # print(f"    🔍 边类型 {edge_type} 使用注意力权重键: {used_key}")
        return attn_weights

    def _process_attention_weights(self, 
                                 attn_weights: torch.Tensor,
                                 edge_index: torch.Tensor,
                                 edge_type: tuple) -> Optional[torch.Tensor]:
        """
        处理注意力权重的维度和多头注意力。
        如果维度不匹配，则返回 None，表示忽略此权重。
        """
        num_edges = edge_index.shape[1]
        
        # 处理多头注意力
        if attn_weights.dim() > 1:
            if attn_weights.shape[-1] > 1:  # 多头注意力
                attn_weights = attn_weights.mean(dim=-1)
            else:
                attn_weights = attn_weights.squeeze(-1)
        
        # 维度验证
        if attn_weights.shape[0] != num_edges:
            print(
                f"    ⚠️ 注意力权重维度不匹配 - 边类型: {edge_type}, "
                f"权重数量: {attn_weights.shape[0]}, 边数量: {num_edges}."
            )
            print(f"    🔧 将忽略此边类型的注意力权重。")
            return None
        
        return attn_weights

    def reset(self, seed: Optional[int] = None) -> Tuple[Dict[str, torch.Tensor], Dict[str, Any]]:
        """
        将环境重置为初始状态
        
        参数:
            seed: 用于可重复性的随机种子
            
        返回:
            observation: 初始状态观察
            info: 附加信息
        """
        if seed is not None:
            torch.manual_seed(seed)
            np.random.seed(seed)
            
        # 使用METIS初始化分区
        initial_partition = self.metis_initializer.initialize_partition(self.num_partitions)
        
        # 使用初始分区重置状态管理器
        self.state_manager.reset(initial_partition)
        
        # 重置环境状态
        self.current_step = 0
        self.episode_history = []
        self.is_terminated = False
        self.is_truncated = False

        # 重置奖励函数状态
        if self.reward_mode == 'dual_layer' and self.reward_function is not None:
            self.reward_function.reset_episode()
        
        # 获取初始观察
        observation = self.state_manager.get_observation()
        
        # 计算初始指标
        initial_metrics = self.evaluator.evaluate_partition(
            self.state_manager.current_partition
        )
        
        # 【新增】在回合开始时，计算并存储初始分区的指标
        self.previous_metrics = initial_metrics
        
        info = {
            'step': self.current_step,
            'metrics': initial_metrics,
            'partition': self.state_manager.current_partition.clone(),
            'boundary_nodes': self.state_manager.get_boundary_nodes(),
            'valid_actions': self.action_space.get_valid_actions(
                self.state_manager.current_partition,
                self.state_manager.get_boundary_nodes()
            )
        }
        
        return observation, info
    
    def _compute_improvement_reward(self, current_metrics: dict, previous_metrics: dict) -> float:
        """
        计算基于"改善程度"的即时奖励 (Delta Reward)。
        奖励的核心是比较当前指标与上一步指标的差异。
        """
        # 硬约束检查：如果破坏了连通性，给予重罚
        if current_metrics.get('connectivity', 1.0) < 1.0:
            return -10.0

        # 【改进】大幅降低进度奖励，从隐式的大奖励变为小激励
        progress_reward = 0.1  # 每个动作的基础奖励，仅作为探索激励

        # 定义各项改善的权重
        improvement_weights = {
            'load_cv': 5.0,      # 降低权重，避免过度激励
            'total_coupling': 2.0,
            'power_balance': 3.0
        }

        # 1. 负荷均衡改善奖励 (load_cv 越低越好，所以 prev - curr > 0 代表改善)
        cv_improvement = previous_metrics.get('load_cv', 1.0) - current_metrics.get('load_cv', 1.0)
        cv_reward = cv_improvement * improvement_weights['load_cv']

        # 2. 耦合度改善奖励 (total_coupling 越低越好)
        coupling_improvement = previous_metrics.get('total_coupling', 1e5) - current_metrics.get('total_coupling', 1e5)
        coupling_reward = coupling_improvement * improvement_weights['total_coupling']

        # 3. 功率平衡改善奖励 (power_imbalance_mean 越低越好)
        pb_improvement = previous_metrics.get('power_imbalance_mean', 1e5) - current_metrics.get('power_imbalance_mean', 1e5)
        pb_reward = pb_improvement * improvement_weights['power_balance']

        # 4. 质量维持奖励 - 如果已经很好了，给予小奖励维持
        quality_maintenance = 0.0
        if current_metrics.get('load_cv', 1.0) < 0.2:
            quality_maintenance = 0.3

        # 5. 时间效率激励 - 早期完成给予额外奖励
        efficiency_bonus = max(0, (self.max_steps - self.current_step) * 0.005)

        # 将所有奖励分量加总
        total_reward = progress_reward + cv_reward + coupling_reward + pb_reward + quality_maintenance + efficiency_bonus
        
        # 将奖励值裁剪到一个合理的范围
        clipped_reward = np.clip(total_reward, -3.0, 2.0)

        return clipped_reward
    
    def _compute_final_bonus(self) -> float:
        """
        计算完成分区后的最终奖励 - 这才是重头戏！
        强化"终局质量"，鼓励智能体追求高质量的最终结果
        """
        current_metrics = self.evaluator.evaluate_partition(self.state_manager.current_partition)
        
        # 基础完成奖励 - 奖励成功完成所有节点分配
        completion_bonus = 15.0
        
        # 质量奖励 - 根据最终分区质量给予额外奖励
        quality_bonus = 0.0
        
        # 负荷均衡奖励（CV越小越好）
        load_cv = current_metrics.get('load_cv', 1.0)
        if load_cv < 0.1:
            quality_bonus += 20.0  # 极佳的负荷平衡
        elif load_cv < 0.2:
            quality_bonus += 10.0
        elif load_cv < 0.3:
            quality_bonus += 5.0
        
        # 低耦合奖励
        total_coupling = current_metrics.get('total_coupling', 1e5)
        inter_region_lines = current_metrics.get('inter_region_lines', 1)
        avg_coupling = total_coupling / max(inter_region_lines, 1)
        if avg_coupling < 0.3:
            quality_bonus += 10.0
        elif avg_coupling < 0.5:
            quality_bonus += 5.0
        
        # 功率平衡奖励
        power_imbalance = current_metrics.get('power_imbalance_mean', 1e5)
        if power_imbalance < 10.0:
            quality_bonus += 8.0
        elif power_imbalance < 50.0:
            quality_bonus += 4.0
        
        # 连通性必须满足
        connectivity = current_metrics.get('connectivity', 1.0)
        if connectivity == 1.0:
            quality_bonus += 5.0
        else:
            # 如果最终状态不连通，严重惩罚
            return -30.0
        
        # 效率奖励 - 用较少步数完成
        efficiency_bonus = 0.0
        if self.current_step < self.max_steps * 0.8:
            efficiency_ratio = 1.0 - (self.current_step / self.max_steps)
            efficiency_bonus = efficiency_ratio * 10.0
        
        total_bonus = completion_bonus + quality_bonus + efficiency_bonus
        
        # 记录详细信息用于调试
        self.final_bonus_components = {
            'completion': completion_bonus,
            'quality': quality_bonus,
            'efficiency': efficiency_bonus,
            'total': total_bonus,
            'metrics': current_metrics
        }
        
        return total_bonus
        
    def step(self, action: Tuple[int, int]) -> Tuple[Dict[str, torch.Tensor], float, bool, bool, Dict[str, Any]]:
        """
        在环境中执行一步
        
        参数:
            action: (node_idx, target_partition)的元组
            
        返回:
            observation: 下一状态观察
            reward: 即时奖励
            terminated: 回合是否终止
            truncated: 回合是否被截断
            info: 附加信息
        """
        if self.is_terminated or self.is_truncated:
            raise RuntimeError("无法在已终止/截断的环境中执行步骤。请先调用reset()。")
            
        # 1. 动作验证 (如果无效，直接返回惩罚)
        if not self.action_space.is_valid_action(
            action, 
            self.state_manager.current_partition,
            self.state_manager.get_boundary_nodes()
        ):
            return self.state_manager.get_observation(), -10.0, True, False, {'termination_reason': 'invalid_action'}
        
        # 2. 执行动作，更新内部状态
        node_idx, target_partition = action
        self.state_manager.update_partition(node_idx, target_partition)
        
        # 3. 计算新状态的指标
        current_metrics = self.evaluator.evaluate_partition(self.state_manager.current_partition)
        
        # 4. 【核心】计算奖励 - 支持三种模式
        if self.reward_mode == 'dual_layer' and self.reward_function is not None:
            # 使用新的双层奖励函数 - 仅计算即时奖励
            reward = self.reward_function.compute_incremental_reward(
                self.state_manager.current_partition,
                action
            )
            # 获取当前指标用于调试
            current_reward_metrics = self.reward_function.get_current_metrics(
                self.state_manager.current_partition
            )
            self.last_reward_components = {
                'incremental_reward': reward,
                'current_metrics': current_reward_metrics,
                'reward_mode': 'dual_layer'
            }
        elif self.reward_mode == 'enhanced' and self.reward_function is not None:
            # 使用增强奖励函数
            reward, reward_components = self.reward_function.compute_reward(
                self.state_manager.current_partition,
                self.state_manager.get_boundary_nodes(),
                action,
                return_components=True
            )
            # 存储奖励组件用于调试和分析
            self.last_reward_components = reward_components
            self.last_reward_components['reward_mode'] = 'enhanced'
        else:
            # 使用原有的增量奖励机制
            reward = self._compute_improvement_reward(current_metrics, self.previous_metrics)
            self.last_reward_components = {
                'improvement_reward': reward,
                'current_metrics': current_metrics,
                'reward_mode': 'legacy'
            }
        
        # 5. 【关键】更新"上一步"的指标，为下一次计算做准备
        self.previous_metrics = current_metrics
        
        # 6. 更新步数和检查终止条件
        self.current_step += 1
        terminated, truncated = self._check_termination()
        
        # 7. 【核心改进】区分结束类型，应用终局奖励
        if terminated or truncated:
            if self.reward_mode == 'dual_layer' and self.reward_function is not None:
                # 使用双层奖励函数计算终局奖励
                termination_type = self._determine_termination_type(terminated, truncated)
                final_reward, final_components = self.reward_function.compute_final_reward(
                    self.state_manager.current_partition,
                    termination_type
                )
                reward += final_reward
                info_bonus = final_components
                info_bonus['termination_type'] = termination_type
            else:
                # 使用原有的终局奖励逻辑
                final_bonus, termination_type = self._apply_final_bonus(terminated, truncated)
                reward += final_bonus

                # 记录终局奖励详情
                if hasattr(self, 'final_bonus_components'):
                    info_bonus = self.final_bonus_components
                    info_bonus['termination_type'] = termination_type
                else:
                    info_bonus = {'termination_type': termination_type, 'final_bonus': final_bonus}
        else:
            info_bonus = {}
        
        # 8. 准备返回信息
        observation = self.state_manager.get_observation()
        
        info = {
            'step': self.current_step,
            'metrics': current_metrics,
            'reward': reward,
            'reward_mode': 'enhanced' if self.use_enhanced_rewards else 'incremental',
            **info_bonus
        }

        # 添加奖励组件信息（如果使用增强奖励）
        if self.last_reward_components is not None:
            info['reward_components'] = self.last_reward_components
        
        return observation, reward, terminated, truncated, info
    
    def _apply_final_bonus(self, terminated: bool, truncated: bool) -> Tuple[float, str]:
        """
        根据结束类型应用不同的终局奖励
        实现"与其慢慢磨蹭赚小钱，不如快速完成拿大奖"的设计哲学
        """
        # 检查是否所有节点都被分配
        unassigned_mask = torch.zeros(self.total_nodes, dtype=torch.bool, device=self.device)
        for i in range(self.total_nodes):
            if self.state_manager.current_partition[i] == 0:  # 0表示未分配
                unassigned_mask[i] = True
        
        unassigned_count = unassigned_mask.sum().item()
        completion_ratio = (self.total_nodes - unassigned_count) / self.total_nodes
        
        if terminated:
            # 检查是否是自然完成
            if unassigned_count == 0:
                # 🎉 自然完成 - 所有节点都被分配，给予最大奖励
                final_bonus = self._compute_final_bonus()
                termination_type = 'natural_completion'
                return final_bonus, termination_type
            else:
                # ⚠️ 提前结束 - 没有有效动作但还有未分配节点
                partial_bonus = self._compute_final_bonus() * completion_ratio * 0.3  # 打30%折扣
                termination_type = 'no_valid_actions'
                return partial_bonus, termination_type
        
        elif truncated:
            # ⏰ 超时结束 - 达到最大步数限制
            if unassigned_count == 0:
                # 虽然超时但完成了所有分配，给予部分奖励
                timeout_bonus = self._compute_final_bonus() * 0.7  # 打70%折扣
                termination_type = 'timeout_completed'
                return timeout_bonus, termination_type
            else:
                # 超时且未完成，轻微惩罚
                timeout_penalty = -5.0 - (1.0 - completion_ratio) * 10.0  # 完成度越低惩罚越重
                termination_type = 'timeout_incomplete'
                return timeout_penalty, termination_type
        
        return 0.0, 'unknown'

    def _determine_termination_type(self, terminated: bool, truncated: bool) -> str:
        """
        确定终止类型，用于双层奖励函数

        Returns:
            'natural': 自然完成（所有节点分配完成）
            'timeout': 超时结束
            'stuck': 提前卡住（无有效动作但未完成）
        """
        if terminated:
            # 检查是否所有节点都被分配
            unassigned_count = (self.state_manager.current_partition == 0).sum().item()
            if unassigned_count == 0:
                return 'natural'
            else:
                return 'stuck'
        elif truncated:
            return 'timeout'
        else:
            return 'unknown'
        
    def _check_termination(self) -> Tuple[bool, bool]:
        """
        检查回合是否应该终止或截断
        
        返回:
            terminated: 自然终止（收敛或无有效动作）
            truncated: 人工终止（达到最大步数）
        """
        # 检查截断（最大步数）
        if self.current_step >= self.max_steps:
            return False, True
            
        # 检查自然终止
        boundary_nodes = self.state_manager.get_boundary_nodes()
        valid_actions = self.action_space.get_valid_actions(
            self.state_manager.current_partition,
            boundary_nodes
        )
        
        # 没有剩余有效动作
        if len(valid_actions) == 0:
            return True, False
            
        # 收敛检查（如果启用）
        if self._check_convergence():
            return True, False
            
        return False, False
        
    def _check_convergence(self, window_size: int = 10, threshold: float = 0.01) -> bool:
        """
        基于最近奖励历史检查分区是否收敛
        
        参数:
            window_size: 要考虑的最近步数
            threshold: 收敛阈值
            
        返回:
            如果收敛返回True，否则返回False
        """
        if len(self.episode_history) < window_size:
            return False
            
        recent_rewards = [step['reward'] for step in self.episode_history[-window_size:]]
        reward_std = np.std(recent_rewards)
        
        return reward_std < threshold
        
    def render(self, mode: str = 'human') -> Optional[np.ndarray]:
        """
        渲染环境的当前状态
        
        参数:
            mode: 渲染模式（'human', 'rgb_array', 或 'ansi'）
            
        返回:
            渲染输出（取决于模式）
        """
        if mode == 'ansi':
            # 基于文本的渲染
            output = []
            output.append(f"步数: {self.current_step}/{self.max_steps}")
            output.append(f"分区数: {self.num_partitions}")
            output.append(f"总节点数: {self.total_nodes}")
            
            # 分区分布
            partition_counts = torch.bincount(
                self.state_manager.current_partition, 
                minlength=self.num_partitions + 1
            )[1:]  # 跳过分区0
            output.append(f"分区大小: {partition_counts.tolist()}")
            
            # 边界节点
            boundary_nodes = self.state_manager.get_boundary_nodes()
            output.append(f"边界节点: {len(boundary_nodes)}")
            
            return '\n'.join(output)
            
        elif mode == 'human':
            print(self.render('ansi'))
            return None
            
        else:
            raise NotImplementedError(f"渲染模式 '{mode}' 未实现")
            
    def close(self):
        """清理环境资源"""
        # 清理缓存数据
        if hasattr(self, 'edge_info'):
            del self.edge_info
        if hasattr(self, 'global_node_mapping'):
            del self.global_node_mapping
            
        # 清理组件引用
        self.state_manager = None
        self.action_space = None
        self.reward_function = None
        self.metis_initializer = None
        self.evaluator = None
        
    def get_action_mask(self) -> torch.Tensor:
        """
        获取当前状态的动作掩码
        
        返回:
            指示有效动作的布尔张量
        """
        return self.action_space.get_action_mask(
            self.state_manager.current_partition,
            self.state_manager.get_boundary_nodes()
        )
        
    def get_state_info(self) -> Dict[str, Any]:
        """
        获取当前状态的详细信息
        
        返回:
            包含状态信息的字典
        """
        return {
            'current_partition': self.state_manager.current_partition.clone(),
            'boundary_nodes': self.state_manager.get_boundary_nodes(),
            'step': self.current_step,
            'max_steps': self.max_steps,
            'num_partitions': self.num_partitions,
            'total_nodes': self.total_nodes,
            'is_terminated': self.is_terminated,
            'is_truncated': self.is_truncated
        }

    def clear_cache(self):
        """清理缓存数据"""
        # 清理缓存数据
        if hasattr(self, 'edge_info'):
            del self.edge_info
        if hasattr(self, 'global_node_mapping'):
            del self.global_node_mapping
            
        # 清理组件引用
        self.state_manager = None
        self.action_space = None
        self.reward_function = None
        self.metis_initializer = None
        self.evaluator = None
