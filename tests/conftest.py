import pytest

from mend import sandbox


@pytest.fixture(scope="session")
def cache(tmp_path_factory):
    """One shared build of the raw databases for the whole test session."""
    return tmp_path_factory.mktemp("mend_cache")


@pytest.fixture(scope="session")
def gold(cache):
    return sandbox.build_gold(cache)
