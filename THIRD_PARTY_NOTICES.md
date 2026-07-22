# Third-Party Notices

This project is distributed under the Apache License, Version 2.0.

## Microsoft ONNX Runtime C API Headers

- **Components**: `native/include/onnxruntime_c_api.h`, `native/include/onnxruntime_ep_c_api.h`
- **Upstream**: Microsoft ONNX Runtime
- **Version / source**: ONNX Runtime v1.26.0
- **License**: MIT License
- **Source URL**: https://github.com/microsoft/onnxruntime/tree/v1.26.0/include
- **Reason included**: ONNX Runtime ABI/header compatibility for native C API integration.

## Local usage notes

- The task binary links against `libonnxruntime` at runtime using an RPATH of `$ORIGIN/lib` and keeps a colocated copy of the linked library.
- Tests and `--self-test` mode do not initialize ONNX Runtime or the model.
