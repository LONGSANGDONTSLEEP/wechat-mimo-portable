$ErrorActionPreference = 'Stop'
python -m PyInstaller --noconfirm --onefile --windowed --name WeChatMiMo --collect-all rapidocr_onnxruntime --collect-binaries onnxruntime --exclude-module onnxruntime.tools --exclude-module onnxruntime.quantization --exclude-module onnxruntime.transformers app.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
