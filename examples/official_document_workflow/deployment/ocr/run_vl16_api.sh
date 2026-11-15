#!/usr/bin/env bash
set -euo pipefail

repo_dir=/home/changdar/Documents/ai-gateway
paddle_env=/home/changdar/.conda/envs/paddleocr_vl_env
cuda_libs="$paddle_env/lib/python3.11/site-packages/nvidia"

export PADDLE_PDX_CACHE_HOME=/home/changdar/.cache/paddleocr-vl
export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True
export PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT=False
export FLAGS_use_mkldnn=0
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=4
cuda_library_path=""
for library_directory in "$cuda_libs"/*/lib; do
  cuda_library_path+="$library_directory:"
done
export LD_LIBRARY_PATH="$cuda_library_path${LD_LIBRARY_PATH:-}"
export LD_PRELOAD="$cuda_libs/cublas/lib/libcublasLt.so.12:$cuda_libs/cublas/lib/libcublas.so.12"

cd "$repo_dir"
exec "$paddle_env/bin/paddlex" --serve \
  --pipeline "$repo_dir/examples/official_document_workflow/deployment/ocr/PaddleOCR-VL-1.6-vllm.yaml" \
  --device gpu:0 --host 127.0.0.1 --port "${PADDLEOCR_VL16_PORT:-8113}"
