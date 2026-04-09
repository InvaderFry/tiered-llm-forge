import sys
import os
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


@pytest.fixture(autouse=True)
def _default_timeout(request):
    """Apply a 60-second timeout to every test unless overridden.

    Individual tests can override with @pytest.mark.timeout(N).
    """
    if request.node.get_closest_marker("timeout") is None:
        request.node.add_marker(pytest.mark.timeout(60))
