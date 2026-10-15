$ErrorActionPreference = 'Stop'
python -m PyInstaller --noconfirm --onefile --windowed --name WeChatStudio --add-data 'web;web' --add-data 'THIRD_PARTY_LICENSES;THIRD_PARTY_LICENSES' --add-data 'LICENSE;.' --collect-all rapidocr_onnxruntime --collect-binaries onnxruntime --collect-submodules Crypto.Cipher --collect-binaries Crypto --exclude-module Crypto.SelfTest --exclude-module onnxruntime.tools --exclude-module onnxruntime.quantization --exclude-module onnxruntime.transformers studio_web.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
