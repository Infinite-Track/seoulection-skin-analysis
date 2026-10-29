"""Train 단계 (Training, GPU) 진입점 — training/train.py를 그대로 실행한다.

SageMaker가 채널·산출물 경로를 환경변수로 준다: SM_CHANNEL_TRAIN(Prepare 출력), SM_CHANNEL_BASE_MODEL(현재 운영 모델 폴더 →
파인튜닝 시작점), SM_MODEL_DIR(model.pt + 서빙 파일 → model.tar.gz). 하이퍼파라미터는 --config, --epochs, --checkpoint-dir … CLI로 온다
(pipelines/sagemaker_pipeline.py hyperparameters). Spot 중단 후 재시작하면 --checkpoint-dir의 last.pt에서 이어간다.
"""
from pipelines.steps.bootstrap import system_libs  # noqa: I001 (common을 import하기 전에)

system_libs()

from training.train import main  # noqa: E402

if __name__ == "__main__":
    main()
