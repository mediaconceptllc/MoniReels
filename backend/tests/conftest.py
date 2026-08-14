"""Global test isolation.

Must run before any test module imports app code: several modules (e.g.
app.stt.chimege_client) call app.utils.logging.get_logger() at import time,
which latches setup_logging()'s file handler onto whatever DATA_DIR is
active at that first call and never re-evaluates it afterwards. If DATA_DIR
still points at the real %APPDATA%/AIVideoEditor dir at that moment, every
test's httpx traffic (even against MockTransport) gets appended to the same
backend.log a real running instance writes to, indistinguishable from
genuine production log lines. Setting it here, at conftest module scope,
runs before pytest imports any test module.
"""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="ai_video_editor_test_")
