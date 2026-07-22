# Third-Party Notices

This project is distributed under the Apache License, Version 2.0.

## Microsoft ONNX Runtime C API Headers

- **Components**: `native/include/onnxruntime_c_api.h`, `native/include/onnxruntime_ep_c_api.h`
- **Upstream**: Microsoft ONNX Runtime
- **Version / source**: ONNX Runtime v1.26.0
- **License**: MIT License
- **Source URL**: https://github.com/microsoft/onnxruntime/tree/v1.26.0/include
- **Reason included**: ONNX Runtime ABI/header compatibility for native C API integration.

### Local usage notes

- The task binary links against `libonnxruntime` at runtime using an RPATH of `$ORIGIN/lib` and keeps a colocated copy of the linked library.
- Tests and `--self-test` mode do not initialize ONNX Runtime or the model.

## Okay Hermes Voice native wakeword tray

The native realtime tray under `native/realtime-tray/` is adapted from the
Okay Hermes Voice native wakeword tray:

- Source: https://github.com/H-Ali13381/okay-hermes-voice
- Revision: `d5ab5026eb9f7f70752d8e67a8df3c42957ed9ff`
- Referenced files: `native/wakeword-tray/main.cpp`, `tray_state.h`, and
  `CMakeLists.txt`
- License: Apache License 2.0

The adapted implementation changes service names, state paths, controller
health handling, menu actions, and browser-launch ownership. The original
project's Apache-2.0 license remains applicable to the adapted portions.

Apache License 2.0: https://www.apache.org/licenses/LICENSE-2.0
