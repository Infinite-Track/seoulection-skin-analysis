"""skin 모델: 공유 백본(ConvNeXt) 하나 + 항목별 헤드. 인자 없이 만들면 skin v2와 같은 구조다.

face_analysis_AI/modeling/v2/model_v2.py와 구조·파라미터 이름이 같다 → v2 체크포인트(deploy_model.pt)의
state_dict를 그대로 불러올 수 있다 (backbone.*, heads.<task>.*, thresholds.<task>).

회귀 항목:   헤드가 숫자 1개를 낸다 (표준화된 값)
순서형 항목: CORAL — K단계 등급을 "등급 > k인가?" 질문 K-1개로 바꾼다.
             헤드 점수 1개 + 질문마다 학습되는 경계값 → 로짓 K-1개. 점수를 공유해서 확률 순서가 뒤집히지 않는다.
출력은 항목들의 로짓을 이어 붙인 (배치, D) 하나. 열 배치는 common.training_targets.output_layout.
"""
import timm
import torch
import torch.nn as nn

from common.training_targets import output_layout
from training.config import TrainConfig, load_tasks

ACTIVATIONS = {"gelu": nn.GELU, "relu": nn.ReLU, "silu": nn.SiLU}


class SkinModel(nn.Module):
    """모든 하이퍼파라미터가 생성자 인자다. 기본값 = skin v2.

    tasks: configs/tasks.yaml의 tasks (None이면 기본 파일을 읽는다)
    backbone: timm 모델 이름. pretrained=True면 ImageNet 가중치를 받는다 (파인튜닝·export 때는 False 후 load_state_dict)
    freeze_stages: 백본 stem + 앞 몇 단계를 고정할지 (0~4). 앞 단계는 경계·질감이라 ImageNet 그대로 둔다
    head_hidden: 헤드 은닉층 노드 수 (0이면 선형 한 층)
    threshold_init: {순서형 항목: 경계값 시작값 K-1개}. 없으면 0
    """

    def __init__(self, tasks=None, backbone="convnext_small.fb_in22k_ft_in1k", pretrained=True, freeze_stages=2,
                 head_hidden=256, head_act="gelu", dropout=0.4, threshold_init=None):
        super().__init__()
        if tasks is None:
            _, tasks = load_tasks()
        if head_act not in ACTIVATIONS:
            raise ValueError(f"head_act는 {sorted(ACTIVATIONS)} 중 하나 ({head_act})")
        self.tasks = list(tasks)
        self.layout = output_layout(tasks)
        self.hparams = dict(backbone=backbone, freeze_stages=freeze_stages, head_hidden=head_hidden,
                            head_act=head_act, dropout=dropout)
        self.backbone = timm.create_model(backbone, pretrained=pretrained, num_classes=0)
        self.heads = nn.ModuleDict({
            task: self._make_head(self.backbone.num_features, head_hidden, head_act, dropout) for task in self.tasks
        })
        init = threshold_init or {}
        self.thresholds = nn.ParameterDict({
            task: nn.Parameter(torch.tensor(init[task], dtype=torch.float32) if task in init
                               else torch.zeros(len(spec["classes"]) - 1))
            for task, spec in tasks.items() if spec["kind"] == "ordinal"
        })
        if freeze_stages:
            self._freeze(freeze_stages)

    @classmethod
    def from_config(cls, cfg: TrainConfig, tasks, pretrained, threshold_init=None, freeze_stages=None):
        return cls(tasks, backbone=cfg.backbone, pretrained=pretrained,
                   freeze_stages=cfg.freeze_stages if freeze_stages is None else freeze_stages,
                   head_hidden=cfg.head_hidden, head_act=cfg.head_act, dropout=cfg.dropout,
                   threshold_init=threshold_init)

    @staticmethod
    def _make_head(in_features, hidden, activation, dropout):
        if not hidden:
            return nn.Linear(in_features, 1)
        return nn.Sequential(nn.Linear(in_features, hidden), ACTIVATIONS[activation](), nn.Dropout(dropout),
                             nn.Linear(hidden, 1))

    def _freeze(self, stages):
        for param in self.backbone.stem.parameters():
            param.requires_grad = False
        for stage in self.backbone.stages[:stages]:
            for param in stage.parameters():
                param.requires_grad = False

    def forward(self, x):
        features = self.backbone(x)  # (배치, num_features)
        outputs = []
        for task in self.tasks:
            score = self.heads[task](features)  # (배치, 1)
            if task in self.thresholds:
                score = score + self.thresholds[task]  # (배치, 1) + (K-1,) → (배치, K-1)
            outputs.append(score)
        return torch.cat(outputs, dim=1)

    def param_groups(self, lr_backbone, lr_head):
        """백본은 작은 학습률(이미 잘 배운 상태), 헤드·경계값은 큰 학습률."""
        backbone = [p for p in self.backbone.parameters() if p.requires_grad]
        head = list(self.heads.parameters()) + list(self.thresholds.parameters())
        return [{"params": backbone, "lr": lr_backbone}, {"params": head, "lr": lr_head}]

    def num_params(self):
        return sum(p.numel() for p in self.parameters())

    def summary(self):
        total, trainable = self.num_params(), sum(p.numel() for p in self.parameters() if p.requires_grad)
        heads = sum(p.numel() for p in self.heads.parameters())
        width = sum(s.stop - s.start for s in self.layout.values())
        return f"전체 {total:,} / 학습 {trainable:,} / 헤드 {heads:,} | 항목 {len(self.tasks)}개 | 출력 {width}열"
