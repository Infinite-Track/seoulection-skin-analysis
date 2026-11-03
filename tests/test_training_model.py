"""training/model.SkinModel — 하이퍼파라미터가 전부 생성자 인자이고, 바꾸면 실제로 반영되며, 인자 없이 만들면 skin v2 구조인지 (Phase 5).
설정 우선순위(TrainConfig 기본값 < yaml < CLI)가 모델 생성까지 이어지는지도 본다. 사전학습 가중치는 받지 않는다 (pretrained=False).
"""
import json

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("timm")

from torch import nn  # noqa: E402

from common.config import REPO_ROOT  # noqa: E402
from common.training_targets import output_layout, output_width  # noqa: E402
from training.config import TrainConfig, build_parser, config_from_args, load_tasks  # noqa: E402
from training.model import ACTIVATIONS, SkinModel  # noqa: E402

V2_META = json.loads((REPO_ROOT / "models/skin_v2/meta.json").read_text(encoding="utf-8"))
TINY = "convnext_atto"  # 3.4M 파라미터, stem·stages 구조는 convnext_small과 같다 (freeze_stages 검사용)


@pytest.fixture(scope="module")
def tasks():
    return load_tasks()[1]


def trainable(params):
    return [p for p in params if p.requires_grad]


def test_default_model_is_skin_v2(tasks):
    """인자 없이 만들면 v2: 백본 이름·파라미터 수(meta.json params)·출력 16열·state_dict 키 체계·앞 2단계 고정."""
    net = SkinModel(pretrained=False)
    assert net.hparams == dict(backbone=V2_META["model"]["backbone"], freeze_stages=2, head_hidden=256, head_act="gelu", dropout=0.4)
    assert net.num_params() == V2_META["model"]["params"] == 50_637_427
    assert net.tasks == list(tasks) and output_width(tasks) == 16
    assert {k.split(".")[0] for k in net.state_dict()} == {"backbone", "heads", "thresholds"}
    assert {k for k in net.state_dict() if k.startswith("thresholds.")} == {f"thresholds.{t}" for t, s in tasks.items() if s["kind"] == "ordinal"}
    for task, spec in tasks.items():
        if spec["kind"] == "ordinal":
            assert net.thresholds[task].shape == (len(spec["classes"]) - 1,) and not net.thresholds[task].any()  # 초기값 0
    assert not trainable(net.backbone.stem.parameters()) and not trainable(net.backbone.stages[0].parameters())
    assert not trainable(net.backbone.stages[1].parameters()) and trainable(net.backbone.stages[2].parameters())
    assert all(p.requires_grad for p in net.heads.parameters()) and all(p.requires_grad for p in net.thresholds.parameters())
    # TrainConfig 기본값도 같은 구조를 만든다
    cfg = TrainConfig()
    assert SkinModel.from_config(cfg, tasks, pretrained=False).hparams == net.hparams
    assert (cfg.backbone, cfg.freeze_stages, cfg.head_hidden, cfg.head_act, cfg.dropout) == tuple(net.hparams.values())


def test_hyperparameter_arguments_change_the_model(tasks):
    base = SkinModel(tasks, backbone=TINY, pretrained=False)
    assert base.hparams["backbone"] == TINY and base.backbone.num_features == 320 and base.num_params() < 5_000_000

    linear = SkinModel(tasks, backbone=TINY, pretrained=False, head_hidden=0)
    assert all(isinstance(h, nn.Linear) and h.in_features == 320 and h.out_features == 1 for h in linear.heads.values())
    assert linear.num_params() < base.num_params()

    custom = SkinModel(tasks, backbone=TINY, pretrained=False, head_hidden=32, head_act="relu", dropout=0.1)
    head = custom.heads["age"]
    assert isinstance(head, nn.Sequential) and head[0].out_features == 32 and isinstance(head[1], nn.ReLU) and head[2].p == 0.1
    assert isinstance(SkinModel(tasks, backbone=TINY, pretrained=False, head_act="silu").heads["age"][1], nn.SiLU)
    with pytest.raises(ValueError, match="head_act"):
        SkinModel(tasks, backbone=TINY, pretrained=False, head_act="tanh")
    assert set(ACTIVATIONS) == {"gelu", "relu", "silu"}

    # freeze_stages: 0이면 전부 학습, 4면 백본 전부 고정 (헤드·경계값은 항상 학습)
    free = SkinModel(tasks, backbone=TINY, pretrained=False, freeze_stages=0)
    assert all(p.requires_grad for p in free.parameters())
    frozen = SkinModel(tasks, backbone=TINY, pretrained=False, freeze_stages=4)
    assert not trainable(frozen.backbone.stem.parameters()) and not any(trainable(s.parameters()) for s in frozen.backbone.stages)
    # 단계 뒤의 최종 norm(backbone.head.norm)은 freeze 범위 밖 — 노트북 model_v2와 같은 규칙 (stem + stages[:N]만 고정)
    assert [n for n, p in frozen.backbone.named_parameters() if p.requires_grad] == ["head.norm.weight", "head.norm.bias"]
    assert trainable(frozen.heads.parameters()) and trainable(frozen.thresholds.parameters())
    one = SkinModel(tasks, backbone=TINY, pretrained=False, freeze_stages=1)
    assert not trainable(one.backbone.stages[0].parameters()) and trainable(one.backbone.stages[1].parameters())

    # threshold_init: 순서형 경계값 시작값
    init = {"pore_grade": [1.5, 0.0, -1.5]}
    seeded = SkinModel(tasks, backbone=TINY, pretrained=False, threshold_init=init)
    assert seeded.thresholds["pore_grade"].tolist() == init["pore_grade"] and not seeded.thresholds["wrinkle_grade"].any()

    # tasks를 줄이면 헤드·출력 열도 준다
    sub = {t: tasks[t] for t in ("age", "pore_grade")}
    small = SkinModel(sub, backbone=TINY, pretrained=False)
    assert list(small.heads) == ["age", "pore_grade"] and list(small.thresholds) == ["pore_grade"] and output_width(sub) == 4


def test_forward_layout_and_coral_thresholds(tasks):
    torch.manual_seed(0)
    net = SkinModel(tasks, backbone=TINY, pretrained=False, freeze_stages=0, head_hidden=8,
                    threshold_init={"wrinkle_grade": [3, 2, 1, 0, -1, -2]}).eval()
    layout = output_layout(tasks)
    with torch.no_grad():
        for w, h in ((192, 160), (512, 576)):  # 부위마다 입력 크기가 다르다 (glabella, cheek)
            x = torch.randn(2, 3, h, w)
            out = net(x)
            assert out.shape == (2, output_width(tasks))
            features = net.backbone(x)
            for task, spec in tasks.items():
                score = net.heads[task](features)
                expected = score + net.thresholds[task] if spec["kind"] == "ordinal" else score
                assert torch.allclose(out[:, layout[task]], expected, atol=1e-6)
            # CORAL: 점수 하나 + 내림차순 경계값 → "등급 > k" 로짓이 k에 대해 단조 감소 (확률 순서가 뒤집히지 않는다)
            wrinkle = out[:, layout["wrinkle_grade"]]
            assert (wrinkle[:, :-1] >= wrinkle[:, 1:]).all()


def test_param_groups_split_backbone_and_heads(tasks):
    net = SkinModel(tasks, backbone=TINY, pretrained=False, freeze_stages=2)
    backbone, head = net.param_groups(lr_backbone=2e-5, lr_head=2e-4)
    assert (backbone["lr"], head["lr"]) == (2e-5, 2e-4)
    assert all(p.requires_grad for p in backbone["params"]) and len(backbone["params"]) == len(trainable(net.backbone.parameters()))
    assert len(head["params"]) == len(list(net.heads.parameters())) + len(list(net.thresholds.parameters()))
    assert "학습" in net.summary() and "출력 16열" in net.summary()


def test_config_priority_default_yaml_cli_reaches_the_model(tmp_path, tasks):
    """기본값 < yaml < CLI. 모르는 yaml 키는 거절. 결과 TrainConfig가 from_config로 모델 구조에 반영된다."""
    yaml_path = tmp_path / "train.yaml"
    yaml_path.write_text("lr_head: 1.0e-4\nepochs: 3\nhead_hidden: 64\nbackbone: convnext_atto\n", encoding="utf-8")
    parser = build_parser("test")
    args = parser.parse_args(["--config", str(yaml_path), "--epochs", "2", "--no-amp", "--views", "F", "--head-hidden", "0",
                              "--dropout", "0.2"])
    cfg = config_from_args(args)
    assert cfg.lr_head == 1e-4 and cfg.backbone == TINY            # yaml
    assert cfg.epochs == 2 and cfg.amp is False and cfg.views == ["F"] and cfg.head_hidden == 0 and cfg.dropout == 0.2  # CLI > yaml
    assert cfg.lr_backbone == TrainConfig().lr_backbone == 2e-5 and cfg.freeze_stages == 2  # 기본값
    net = SkinModel.from_config(cfg, tasks, pretrained=False)
    assert net.hparams == dict(backbone=TINY, freeze_stages=2, head_hidden=0, head_act="gelu", dropout=0.2)
    assert isinstance(net.heads["age"], nn.Linear)
    assert SkinModel.from_config(cfg, tasks, pretrained=False, freeze_stages=0).hparams["freeze_stages"] == 0  # export 때 쓰는 덮어쓰기

    (tmp_path / "bad.yaml").write_text("learning_rate: 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="learning_rate"):
        config_from_args(parser.parse_args(["--config", str(tmp_path / "bad.yaml")]))
    with pytest.raises(FileNotFoundError):
        config_from_args(parser.parse_args(["--config", str(tmp_path / "missing.yaml")]))
    assert "datasets" not in vars(parser.parse_args([]))  # 구조가 있는 필드는 yaml로만
