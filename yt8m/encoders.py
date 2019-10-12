import torch
import torch.nn as nn
import torch.nn.functional as F


def general_weight_initialization(module: nn.Module):
    if isinstance(module, (nn.BatchNorm1d, nn.BatchNorm2d)):
        if module.weight is not None:
            nn.init.uniform_(module.weight)
        if module.bias is not None:
            nn.init.constant_(module.bias, 0)
    elif isinstance(module, nn.Linear):
        nn.init.kaiming_normal_(module.weight)
        print("Initing linear")
        if module.bias is not None:
            nn.init.constant_(module.bias, 0)


class TimeFirstBatchNorm1d(nn.Module):
    def __init__(self, dim, groups=None):
        super().__init__()
        self.groups = groups
        self.bn = nn.BatchNorm1d(dim)

    def forward(self, tensor):
        _, length, dim = tensor.size()
        if self.groups:
            dim = dim // self.groups
        tensor = tensor.view(-1, dim)
        tensor = self.bn(tensor)
        if self.groups:
            return tensor.view(-1, length, self.groups, dim)
        else:
            return tensor.view(-1, length, dim)


class LambdaLayer(nn.Module):
    def __init__(self, func):
        super().__init__()
        self.func = func

    def forward(self, tensor):
        return self.func(tensor)


def permute_tensor(permute):
    def _permute_tensor(tensor):
        return tensor.permute(*permute)
    return _permute_tensor


def view_tensor(view):
    def _view_tensor(tensor):
        return tensor.view(*view)
    return _view_tensor


class SEModule(nn.Module):
    """Squeeze-and-excite context gating
    Adapted from https://github.com/Cadene/pretrained-models.pytorch/blob/master/pretrainedmodels/models/senet.py
    """

    def __init__(self, channels, reduction):
        super().__init__()
        self.fc1 = nn.Linear(channels, channels // reduction)
        self.relu = nn.ReLU()
        self.fc2 = nn.Linear(channels // reduction, channels)
        self.sigmoid = nn.Sigmoid()
        self._init_weights()

    def _init_weights(self):
        for module in self.modules():
            general_weight_initialization(module)

    def forward(self, x):
        module_input = x
        x = self.fc1(x)
        x = self.relu(x)
        x = self.fc2(x)
        x = self.sigmoid(x)
        return module_input * x


class BNSEModule(nn.Module):
    """Squeeze-and-excite context gating
    Adapted from https://github.com/Cadene/pretrained-models.pytorch/blob/master/pretrainedmodels/models/senet.py
    """

    def __init__(self, channels, reduction):
        super().__init__()
        self.bn1 = nn.BatchNorm1d(channels)
        self.fc1 = nn.Linear(channels, channels // reduction)
        self.relu = nn.ReLU()
        self.bn2 = nn.BatchNorm1d(channels // reduction)
        self.fc2 = nn.Linear(channels // reduction, channels)
        self.sigmoid = nn.Sigmoid()
        self._init_weights()

    def _init_weights(self):
        for module in self.modules():
            general_weight_initialization(module)

    def forward(self, x):
        module_input = x
        x = self.bn1(x)
        x = self.fc1(x)
        x = self.relu(x)
        x = self.bn2(x)
        x = self.fc2(x)
        x = self.sigmoid(x)
        return module_input * x


class BNSE1dModule(nn.Module):
    """Squeeze-and-excite context gating
    Adapted from https://github.com/Cadene/pretrained-models.pytorch/blob/master/pretrainedmodels/models/senet.py
    """

    def __init__(self, channels, reduction):
        super().__init__()
        self.bn1 = nn.BatchNorm1d(channels)
        self.fc1 = nn.Conv1d(channels, channels // reduction, kernel_size=1)
        self.relu = nn.ReLU()
        self.bn2 = nn.BatchNorm1d(channels // reduction)
        self.fc2 = nn.Conv1d(channels // reduction, channels, kernel_size=1)
        self.sigmoid = nn.Sigmoid()
        self._init_weights()

    def _init_weights(self):
        for module in self.modules():
            general_weight_initialization(module)

    def forward(self, x):
        module_input = x
        x = self.bn1(x)
        x = self.fc1(x)
        x = self.relu(x)
        x = self.bn2(x)
        x = self.fc2(x)
        x = self.sigmoid(x)
        return module_input * x


class NetVLAD(nn.Module):
    """NetVLAD layer implementation

    Adapted from https://github.com/lyakaap/NetVLAD-pytorch/blob/master/netvlad.py
    """

    def __init__(self, num_clusters=64, dim=128, alpha=100.0,
                 normalize_input=True, p_drop=0.25, add_batchnorm=False):
        """
        Args:
            num_clusters : int
                The number of clusters
            dim : int
                Dimension of descriptors
            alpha : float
                Parameter of initialization. Larger value is harder assignment.
            normalize_input : bool
                If true, descriptor-wise L2 normalization is applied to input.
        """
        super().__init__()
        self.p_drop = p_drop
        self.cluster_dropout = nn.Dropout2d(p_drop)
        self.num_clusters = num_clusters
        self.dim = dim
        self.alpha = alpha
        self.normalize_input = normalize_input
        self.add_batchnorm = add_batchnorm
        if add_batchnorm:
            self.soft_assignment_mapper = nn.Sequential(
                nn.Linear(dim, num_clusters, bias=False),
                TimeFirstBatchNorm1d(num_clusters)
            )
        else:
            self.soft_assignment_mapper = nn.Linear(
                dim, num_clusters, bias=True)
        self.centroids = nn.Parameter(torch.rand(num_clusters, dim))
        self._init_params()

    def _init_params(self):
        if self.add_batchnorm:
            self.soft_assignment_mapper[0].weight = nn.Parameter(
                (2.0 * self.alpha * self.centroids)
            )
            nn.init.constant_(self.soft_assignment_mapper[1].bn.weight, 1)
            nn.init.constant_(self.soft_assignment_mapper[1].bn.bias, 0)
        else:
            self.soft_assignment_mapper.weight = nn.Parameter(
                (2.0 * self.alpha * self.centroids)
            )
            self.soft_assignment_mapper.bias = nn.Parameter(
                - self.alpha * self.centroids.norm(dim=1)
            )

    def forward(self, x, masks=None):
        """NetVlad Adaptive Pooling

        Arguments:
            x {torch.Tensor} -- shape: (n_batch, len, dim)

        Returns:
            torch.Tensor -- shape (n_batch, n_cluster * dim)
        """
        N, C = x.shape[:2]

        if self.normalize_input:
            x = F.normalize(x, p=2, dim=2)  # across descriptor dim

        # soft-assignment
        # shape: (n_batch, len, n_cluster)
        soft_assign = self.soft_assignment_mapper(x)
        soft_assign = F.softmax(soft_assign, dim=2)

        # calculate residuals to each clusters
        if masks is not None:
            # shape: (n_batch, len, n_cluster)
            soft_assign = soft_assign * masks[:, :, None]
        second_term = (
            soft_assign.sum(dim=1).unsqueeze(2) * self.centroids[None, :, :]
        )
        first_term = (soft_assign.unsqueeze(3) * x.unsqueeze(2)).sum(dim=1)

        # vlad shape (n_batch, n_cluster, dim)
        vlad = first_term - second_term
        vlad = F.normalize(vlad, p=2, dim=2)  # intra-normalization
        # flatten shape (n_batch, n_cluster * dim / groups)
        vlad = vlad.view(x.size(0), -1)  # flatten
        vlad = F.normalize(vlad, p=2, dim=1)  # L2 normalize
        if self.p_drop:
            vlad = self.cluster_dropout(
                vlad.view(x.size(0), self.num_clusters, self.dim, 1)
            ).view(x.size(0), -1)
        return vlad


class NeXtVLAD(nn.Module):
    """NeXtVLAD layer implementation

    Adapted from https://github.com/linrongc/youtube-8m/blob/master/nextvlad.py
    """

    def __init__(self, num_clusters=64, dim=128, alpha=100.0,
                 groups: int = 8, expansion: int = 2,
                 normalize_input=True, p_drop=0.25, add_batchnorm=False):
        """
        Args:
            num_clusters : int
                The number of clusters
            dim : int
                Dimension of descriptors
            alpha : float
                Parameter of initialization. Larger value is harder assignment.
            normalize_input : bool
                If true, descriptor-wise L2 normalization is applied to input.
        """
        super().__init__()
        assert dim % groups == 0, "`dim` must be divisible by `groups`"
        assert expansion > 1
        self.p_drop = p_drop
        self.cluster_dropout = nn.Dropout2d(p_drop)
        self.num_clusters = num_clusters
        self.dim = dim
        self.expansion = expansion
        self.grouped_dim = dim * expansion // groups
        self.groups = groups
        self.alpha = alpha
        self.normalize_input = normalize_input
        self.add_batchnorm = add_batchnorm
        self.expansion_mapper = nn.Linear(dim, dim * expansion)
        if add_batchnorm:
            self.soft_assignment_mapper = nn.Sequential(
                nn.Linear(dim * expansion, num_clusters * groups, bias=False),
                TimeFirstBatchNorm1d(num_clusters, groups=groups)
            )
        else:
            self.soft_assignment_mapper = nn.Linear(
                dim * expansion, num_clusters * groups, bias=True)
        self.attention_mapper = nn.Linear(
            dim * expansion, groups
        )
        # (n_clusters, dim / group)
        self.centroids = nn.Parameter(
            torch.rand(num_clusters, self.grouped_dim))
        self.final_bn = nn.BatchNorm1d(num_clusters * self.grouped_dim)
        self._init_params()

    def _init_params(self):
        for component in (self.soft_assignment_mapper, self.attention_mapper,
                          self.expansion_mapper):
            for module in component.modules():
                general_weight_initialization(module)
        if self.add_batchnorm:
            self.soft_assignment_mapper[0].weight = nn.Parameter(
                (2.0 * self.alpha * self.centroids).repeat((self.groups, self.groups))
            )
            nn.init.constant_(self.soft_assignment_mapper[1].bn.weight, 1)
            nn.init.constant_(self.soft_assignment_mapper[1].bn.bias, 0)
        else:
            self.soft_assignment_mapper.weight = nn.Parameter(
                (2.0 * self.alpha * self.centroids).repeat((self.groups, self.groups))
            )
            self.soft_assignment_mapper.bias = nn.Parameter(
                (- self.alpha * self.centroids.norm(dim=1)
                 ).repeat((self.groups,))
            )

    def forward(self, x, masks=None):
        """NeXtVlad Adaptive Pooling

        Arguments:
            x {torch.Tensor} -- shape: (n_batch, len, dim)

        Returns:
            torch.Tensor -- shape (n_batch, n_cluster * dim / groups)
        """
        N, C = x.shape[:2]

        if self.normalize_input:
            x = F.normalize(x, p=2, dim=2)  # across descriptor dim

        # expansion
        # shape: (n_batch, len, dim * expansion)
        x = self.expansion_mapper(x)

        # soft-assignment
        # shape: (n_batch, len, n_cluster, groups)
        soft_assign = self.soft_assignment_mapper(x).view(
            x.size(0), x.size(1), self.num_clusters, self.groups
        )
        soft_assign = F.softmax(soft_assign, dim=2)

        # attention
        # shape: (n_batch, len, groups)
        attention = torch.sigmoid(self.attention_mapper(x))
        if masks is not None:
            # shape: (n_batch, len, groups)
            attention = attention * masks[:, :, None]

        # (n_batch, len, n_cluster, groups, dim / groups)
        activation = (
            attention[:, :, None, :, None] *
            soft_assign[:, :, :, :, None]
        )

        # calculate residuals to each clusters
        # (n_batch, n_cluster, dim / groups)
        second_term = (
            activation.sum(dim=3).sum(dim=1) *
            self.centroids[None, :, :]
        )
        # (n_batch, n_cluster, dim / groups)
        first_term = (
            # (n_batch, len, n_cluster, groups, dim / groups)
            activation *
            x.view(x.size(0), x.size(1), 1, self.groups, self.grouped_dim)
        ).sum(dim=3).sum(dim=1)

        # vlad shape (n_batch, n_cluster, dim / groups)
        vlad = first_term - second_term
        vlad = F.normalize(vlad, p=2, dim=2)  # intra-normalization
        # flatten shape (n_batch, n_cluster * dim / groups)
        vlad = vlad.view(x.size(0), -1)  # flatten
        # vlad = F.normalize(vlad, p=2, dim=1)  # L2 normalize
        vlad = self.final_bn(vlad)
        if self.p_drop:
            vlad = self.cluster_dropout(
                vlad.view(x.size(0), self.num_clusters, self.grouped_dim, 1)
            ).view(x.size(0), -1)
        return vlad


def test_nextvlad():
    model = NeXtVLAD(
        num_clusters=64, dim=128, alpha=100,
        groups=8, expansion=2, normalize_input=True,
        p_drop=0.25, add_batchnorm=True
    )
    # shape (n_batch, len, dim)
    input_tensor = torch.rand(16, 300, 128)
    # shape (n_batch, n_clusters * dim / groups)
    output_tensor = model(input_tensor)
    assert output_tensor.size() == (16, 64 * 2 * 128 // 8)
    model = NeXtVLAD(
        num_clusters=64, dim=128, alpha=100,
        groups=8, expansion=2, normalize_input=True,
        p_drop=0.25, add_batchnorm=False
    )
    # shape (n_batch, len, dim)
    input_tensor = torch.rand(16, 300, 128)
    # shape (n_batch, n_clusters * dim / groups)
    output_tensor = model(input_tensor)
    assert output_tensor.size() == (16, 64 * 2 * 128 // 8)


def test_netvlad():
    model = NetVLAD(
        num_clusters=64, dim=128, alpha=100,
        normalize_input=True,
        p_drop=0.25, add_batchnorm=True
    )
    # shape (n_batch, len, dim)
    input_tensor = torch.rand(16, 300, 128)
    # shape (n_batch, n_clusters * dim )
    output_tensor = model(input_tensor)
    assert output_tensor.size() == (16, 64 * 128)
    model = NetVLAD(
        num_clusters=64, dim=128, alpha=100,
        normalize_input=True,
        p_drop=0.25, add_batchnorm=False
    )
    # shape (n_batch, len, dim)
    input_tensor = torch.rand(16, 300, 128)
    # shape (n_batch, n_clusters * dim)
    output_tensor = model(input_tensor)
    assert output_tensor.size() == (16, 64 * 128)


if __name__ == "__main__":
    test_netvlad()
    test_nextvlad()
