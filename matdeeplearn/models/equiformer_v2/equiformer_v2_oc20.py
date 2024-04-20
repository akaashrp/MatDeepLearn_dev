import logging
import math
from typing import List, Optional

import torch
import torch.nn as nn

import torch_geometric

from matdeeplearn.common.registry import registry
from matdeeplearn.models.base_model import BaseModel, conditional_grad

try:
    pass
except ImportError:
    pass

from .smearing import GaussianSmearing

from .edge_rot_mat import init_edge_rot_mat
from .gaussian_rbf import GaussianRadialBasisLayer
from .input_block import EdgeDegreeEmbedding
from .layer_norm import (
    EquivariantLayerNormArray,
    EquivariantLayerNormArraySphericalHarmonics,
    EquivariantRMSNormArraySphericalHarmonics,
    EquivariantRMSNormArraySphericalHarmonicsV2,
    get_normalization_layer,
)
from .module_list import ModuleListInfo
from .radial_function import RadialFunction
from .so3 import (
    CoefficientMappingModule,
    SO3_Embedding,
    SO3_Grid,
    SO3_LinearV2,
    SO3_Rotation,
)
from .transformer_block import (
    FeedForwardNetwork,
    SO2EquivariantGraphAttention,
    TransBlockV2,
)

# Statistics of IS2RE 100K
_AVG_NUM_NODES = 77.81317
_AVG_DEGREE = (
    23.395238876342773  # IS2RE: 100k, max_radius = 5, max_neighbors = 100
)


@registry.register_model("equiformer_v2")
class EquiformerV2_OC20(BaseModel):
    """
    Equiformer with graph attention built upon SO(2) convolution and feedforward network built upon S2 activation

    Args:
        num_layers (int):             Number of layers in the GNN
        sphere_channels (int):        Number of spherical channels (one set per resolution)
        attn_hidden_channels (int): Number of hidden channels used during SO(2) graph attention
        num_heads (int):            Number of attention heads
        attn_alpha_head (int):      Number of channels for alpha vector in each attention head
        attn_value_head (int):      Number of channels for value vector in each attention head
        ffn_hidden_channels (int):  Number of hidden channels used during feedforward network
        norm_type (str):            Type of normalization layer (['layer_norm', 'layer_norm_sh', 'rms_norm_sh'])

        lmax_list (int):              List of maximum degree of the spherical harmonics (1 to 10)
        mmax_list (int):              List of maximum order of the spherical harmonics (0 to lmax)
        grid_resolution (int):        Resolution of SO3_Grid

        num_sphere_samples (int):     Number of samples used to approximate the integration of the sphere in the output blocks

        edge_channels (int):                Number of channels for the edge invariant features
        use_atom_edge_embedding (bool):     Whether to use atomic embedding along with relative distance for edge scalar features
        share_atom_edge_embedding (bool):   Whether to share `atom_edge_embedding` across all blocks
        use_m_share_rad (bool):             Whether all m components within a type-L vector of one channel share radial function weights
        distance_function ("gaussian", "sigmoid", "linearsigmoid", "silu"):  Basis function used for distances

        attn_activation (str):      Type of activation function for SO(2) graph attention
        use_s2_act_attn (bool):     Whether to use attention after S2 activation. Otherwise, use the same attention as Equiformer
        use_attn_renorm (bool):     Whether to re-normalize attention weights
        ffn_activation (str):       Type of activation function for feedforward network
        use_gate_act (bool):        If `True`, use gate activation. Otherwise, use S2 activation
        use_grid_mlp (bool):        If `True`, use projecting to grids and performing MLPs for FFNs.
        use_sep_s2_act (bool):      If `True`, use separable S2 activation when `use_gate_act` is False.

        alpha_drop (float):         Dropout rate for attention weights
        drop_path_rate (float):     Drop path rate
        proj_drop (float):          Dropout rate for outputs of attention and FFN in Transformer blocks

        weight_init (str):          ['normal', 'uniform'] initialization of weights of linear layers except those in radial functions
        avg_num_nodes (float):      Average number of nodes per graph
        avg_degree (float):         Average degree of nodes in the graph

    """

    def __init__(
        self,
	      node_dim,
        edge_dim,
        output_dim,
        max_num_elements: int = 100,
        num_rbf=600,
        num_layers: int = 12,
        pool="global_mean_pool",
        sphere_channels: int = 128,
        attn_hidden_channels: int = 128,
        num_heads: int = 8,
        attn_alpha_channels: int = 32,
        attn_value_channels: int = 16,
        ffn_hidden_channels: int = 512,
        norm_type: str = "rms_norm_sh",
        lmax_list: List[int] = [4],
        mmax_list: List[int] = [2],
        grid_resolution: Optional[int] = None,
        num_sphere_samples: int = 128,
        edge_channels: int = 128,
        use_atom_edge_embedding: bool = True,
        share_atom_edge_embedding: bool = False,
        use_m_share_rad: bool = False,
        distance_function: str = "gaussian",
        num_distance_basis: int = 128,
        attn_activation: str = "scaled_silu",
        use_s2_act_attn: bool = False,
        use_attn_renorm: bool = True,
        ffn_activation: str = "scaled_silu",
        use_gate_act: bool = False,
        use_grid_mlp: bool = False,
        use_sep_s2_act: bool = True,
        alpha_drop: float = 0.1,
        drop_path_rate: float = 0.05,
        proj_drop: float = 0.0,
        weight_init: str = "normal",
        avg_num_nodes: Optional[float] = None,
        avg_degree: Optional[float] = None,
        **kwargs
    ):
        super(EquiformerV2_OC20, self).__init__(**kwargs)

        import sys

        if "e3nn" not in sys.modules:
            logging.error(
                "You need to install e3nn==0.4.4 to use EquiformerV2."
            )
            raise ImportError

        self.output_dim = output_dim
        self.pool = pool
        self.max_num_elements = max_num_elements
        self.num_rbf = num_rbf

        self.num_layers = num_layers
        self.sphere_channels = sphere_channels
        self.attn_hidden_channels = attn_hidden_channels
        self.num_heads = num_heads
        self.attn_alpha_channels = attn_alpha_channels
        self.attn_value_channels = attn_value_channels
        self.ffn_hidden_channels = ffn_hidden_channels
        self.norm_type = norm_type

        self.lmax_list = lmax_list
        self.mmax_list = mmax_list
        self.grid_resolution = grid_resolution

        self.num_sphere_samples = num_sphere_samples

        self.edge_channels = edge_channels
        self.use_atom_edge_embedding = use_atom_edge_embedding
        self.share_atom_edge_embedding = share_atom_edge_embedding
        if self.share_atom_edge_embedding:
            assert self.use_atom_edge_embedding
            self.block_use_atom_edge_embedding = False
        else:
            self.block_use_atom_edge_embedding = self.use_atom_edge_embedding
        self.use_m_share_rad = use_m_share_rad
        self.distance_function = distance_function
        self.num_distance_basis = num_distance_basis

        self.attn_activation = attn_activation
        self.use_s2_act_attn = use_s2_act_attn
        self.use_attn_renorm = use_attn_renorm
        self.ffn_activation = ffn_activation
        self.use_gate_act = use_gate_act
        self.use_grid_mlp = use_grid_mlp
        self.use_sep_s2_act = use_sep_s2_act

        self.alpha_drop = alpha_drop
        self.drop_path_rate = drop_path_rate
        self.proj_drop = proj_drop

        self.avg_num_nodes = avg_num_nodes or _AVG_NUM_NODES
        self.avg_degree = avg_degree or _AVG_DEGREE

        self.weight_init = weight_init
        assert self.weight_init in ["normal", "uniform"]

        self.device = "cpu"  # torch.cuda.current_device()

        self.grad_forces = False
        self.num_resolutions: int = len(self.lmax_list)
        self.sphere_channels_all: int = (
            self.num_resolutions * self.sphere_channels
        )

        # Weights for message initialization
        self.sphere_embedding = nn.Embedding(
            self.max_num_elements, self.sphere_channels_all
        )

        # Initialize the function used to measure the distances between atoms
        assert self.distance_function in [
            "gaussian",
        ]
        if self.distance_function == "gaussian":
            self.distance_expansion = GaussianSmearing(
                0.0,
                self.cutoff_radius,
                num_rbf,
                2.0,
            )
            # self.distance_expansion = GaussianRadialBasisLayer(num_basis=self.num_distance_basis, cutoff=self.max_radius)
        else:
            raise ValueError

        # Initialize the sizes of radial functions (input channels and 2 hidden channels)
        self.edge_channels_list = [int(self.distance_expansion.num_output)] + [
            self.edge_channels
        ] * 2

        # Initialize atom edge embedding
        if self.share_atom_edge_embedding and self.use_atom_edge_embedding:
            self.source_embedding = nn.Embedding(
                self.max_num_elements, self.edge_channels_list[-1]
            )
            self.target_embedding = nn.Embedding(
                self.max_num_elements, self.edge_channels_list[-1]
            )
            self.edge_channels_list[0] = (
                self.edge_channels_list[0] + 2 * self.edge_channels_list[-1]
            )
        else:
            self.source_embedding, self.target_embedding = None, None

        # Initialize the module that compute WignerD matrices and other values for spherical harmonic calculations
        self.SO3_rotation = nn.ModuleList()
        for i in range(self.num_resolutions):
            self.SO3_rotation.append(SO3_Rotation(self.lmax_list[i]))

        # Initialize conversion between degree l and order m layouts
        self.mappingReduced = CoefficientMappingModule(
            self.lmax_list, self.mmax_list
        )

        # Initialize the transformations between spherical and grid representations
        self.SO3_grid = ModuleListInfo(
            "({}, {})".format(max(self.lmax_list), max(self.lmax_list))
        )
        for lval in range(max(self.lmax_list) + 1):
            SO3_m_grid = nn.ModuleList()
            for m in range(max(self.lmax_list) + 1):
                SO3_m_grid.append(
                    SO3_Grid(
                        lval,
                        m,
                        resolution=self.grid_resolution,
                        normalization="component",
                    )
                )
            self.SO3_grid.append(SO3_m_grid)

        # Edge-degree embedding
        self.edge_degree_embedding = EdgeDegreeEmbedding(
            self.sphere_channels,
            self.lmax_list,
            self.mmax_list,
            self.SO3_rotation,
            self.mappingReduced,
            self.max_num_elements,
            self.edge_channels_list,
            self.block_use_atom_edge_embedding,
            rescale_factor=self.avg_degree,
        )

        # Initialize the blocks for each layer of EquiformerV2
        self.blocks = nn.ModuleList()
        for i in range(self.num_layers):
            block = TransBlockV2(
                self.sphere_channels,
                self.attn_hidden_channels,
                self.num_heads,
                self.attn_alpha_channels,
                self.attn_value_channels,
                self.ffn_hidden_channels,
                self.sphere_channels,
                self.lmax_list,
                self.mmax_list,
                self.SO3_rotation,
                self.mappingReduced,
                self.SO3_grid,
                self.max_num_elements,
                self.edge_channels_list,
                self.block_use_atom_edge_embedding,
                self.use_m_share_rad,
                self.attn_activation,
                self.use_s2_act_attn,
                self.use_attn_renorm,
                self.ffn_activation,
                self.use_gate_act,
                self.use_grid_mlp,
                self.use_sep_s2_act,
                self.norm_type,
                self.alpha_drop,
                self.drop_path_rate,
                self.proj_drop,
            )
            self.blocks.append(block)

        # Output blocks for energy and forces
        self.norm = get_normalization_layer(
            self.norm_type,
            lmax=max(self.lmax_list),
            num_channels=self.sphere_channels,
        )

        self.output_block = FeedForwardNetwork(
            self.sphere_channels,
            self.ffn_hidden_channels,
            self.output_dim,
            self.lmax_list,
            self.mmax_list,
            self.SO3_grid,
            self.ffn_activation,
            self.use_gate_act,
            self.use_grid_mlp,
            self.use_sep_s2_act,
        )

        self.apply(self._init_weights)
        self.apply(self._uniform_init_rad_func_linear_weights)

    @conditional_grad(torch.enable_grad())
    def _forward(self, data):
        self.batch_size = len(data.n_atoms)
        self.dtype = data.pos.dtype
        self.device = data.pos.device

        atomic_numbers = data.z.long()
        num_atoms = len(atomic_numbers)

        #if self.otf_edge_index == True:
        edge_index, edge_distance, edge_distance_vec, cell_offsets, _, neighbors = self.generate_graph(data, self.cutoff_radius, self.n_neighbors)  
                
        ###############################################################
        # Initialize data structures
        ###############################################################

        # Compute 3x3 rotation matrix per edge
        edge_rot_mat = self._init_edge_rot_mat(
            data, edge_index, edge_distance_vec
        )

        # Initialize the WignerD matrices and other values for spherical harmonic calculations
        for i in range(self.num_resolutions):
            self.SO3_rotation[i].set_wigner(edge_rot_mat)

        ###############################################################
        # Initialize node embeddings
        ###############################################################

        # Init per node representations using an atomic number based embedding
        offset = 0
        x = SO3_Embedding(
            num_atoms,
            self.lmax_list,
            self.sphere_channels,
            self.device,
            self.dtype,
        )

        offset_res = 0
        offset = 0
        # Initialize the l = 0, m = 0 coefficients for each resolution
        for i in range(self.num_resolutions):
            if self.num_resolutions == 1:
                x.embedding[:, offset_res, :] = self.sphere_embedding(
                    atomic_numbers
                )
            else:
                x.embedding[:, offset_res, :] = self.sphere_embedding(
                    atomic_numbers
                )[:, offset : offset + self.sphere_channels]
            offset = offset + self.sphere_channels
            offset_res = offset_res + int((self.lmax_list[i] + 1) ** 2)

        # Edge encoding (distance and atom edge)
        edge_distance = self.distance_expansion(edge_distance)
        if self.share_atom_edge_embedding and self.use_atom_edge_embedding:
            source_element = atomic_numbers[
                edge_index[0]
            ]  # Source atom atomic number
            target_element = atomic_numbers[
                edge_index[1]
            ]  # Target atom atomic number
            source_embedding = self.source_embedding(source_element)
            target_embedding = self.target_embedding(target_element)
            edge_distance = torch.cat(
                (edge_distance, source_embedding, target_embedding), dim=1
            )

        # Edge-degree embedding
        edge_degree = self.edge_degree_embedding(
            atomic_numbers, edge_distance, edge_index
        )
        x.embedding = x.embedding + edge_degree.embedding

        ###############################################################
        # Update spherical node embeddings
        ###############################################################

        for i in range(self.num_layers):
            x = self.blocks[i](
                x,  # SO3_Embedding
                atomic_numbers,
                edge_distance,
                edge_index,
                batch=data.batch,  # for GraphDropPath
            )

        # Final layer norm
        x.embedding = self.norm(x.embedding)
        outputs = {}
        out = self.output_block(x)
        out = out.embedding.narrow(1, 0, 1).squeeze()
        out = getattr(torch_geometric.nn, self.pool)(out, data.batch)
        out = out.view(-1, 1)
        return out

    def forward(self, data):
    
        output = {}
        out = self._forward(data)
        output["output"] =  out

        if self.gradient == True and out.requires_grad == True:         
            volume = torch.einsum("zi,zi->z", data.cell[:, 0, :], torch.cross(data.cell[:, 1, :], data.cell[:, 2, :], dim=1)).unsqueeze(-1)                        
            grad = torch.autograd.grad(
                    out,
                    [data.pos, data.displacement],
                    grad_outputs=torch.ones_like(out),
                    create_graph=self.training) 
            forces = -1 * grad[0]
            stress = grad[1]
            stress = stress / volume.view(-1, 1, 1)         

            output["pos_grad"] =  forces
            output["cell_grad"] =  stress
        else:
            output["pos_grad"] =  None
            output["cell_grad"] =  None  
                  
        return output  

    # Initialize the edge rotation matrics
    def _init_edge_rot_mat(self, data, edge_index, edge_distance_vec):
        return init_edge_rot_mat(edge_distance_vec)

    @property
    def num_params(self):
        return sum(p.numel() for p in self.parameters())

    def _init_weights(self, m):
        if isinstance(m, torch.nn.Linear) or isinstance(m, SO3_LinearV2):
            if m.bias is not None:
                torch.nn.init.constant_(m.bias, 0)
            if self.weight_init == "normal":
                std = 1 / math.sqrt(m.in_features)
                torch.nn.init.normal_(m.weight, 0, std)

        elif isinstance(m, torch.nn.LayerNorm):
            torch.nn.init.constant_(m.bias, 0)
            torch.nn.init.constant_(m.weight, 1.0)

    def _uniform_init_rad_func_linear_weights(self, m):
        if isinstance(m, RadialFunction):
            m.apply(self._uniform_init_linear_weights)

    def _uniform_init_linear_weights(self, m):
        if isinstance(m, torch.nn.Linear):
            if m.bias is not None:
                torch.nn.init.constant_(m.bias, 0)
            std = 1 / math.sqrt(m.in_features)
            torch.nn.init.uniform_(m.weight, -std, std)

    @torch.jit.ignore
    def no_weight_decay(self) -> set:
        no_wd_list = []
        named_parameters_list = [name for name, _ in self.named_parameters()]
        for module_name, module in self.named_modules():
            if isinstance(
                module,
                (
                    torch.nn.Linear,
                    SO3_LinearV2,
                    torch.nn.LayerNorm,
                    EquivariantLayerNormArray,
                    EquivariantLayerNormArraySphericalHarmonics,
                    EquivariantRMSNormArraySphericalHarmonics,
                    EquivariantRMSNormArraySphericalHarmonicsV2,
                    GaussianRadialBasisLayer,
                ),
            ):
                for parameter_name, _ in module.named_parameters():
                    if isinstance(module, (torch.nn.Linear, SO3_LinearV2)):
                        if "weight" in parameter_name:
                            continue
                    global_parameter_name = module_name + "." + parameter_name
                    assert global_parameter_name in named_parameters_list
                    no_wd_list.append(global_parameter_name)

        return set(no_wd_list)

    @property
    def target_attr(self):
        return "y"
