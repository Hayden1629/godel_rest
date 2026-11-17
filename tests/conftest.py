def pytest_configure(config):
    config.addinivalue_line("markers", "unit: fast, isolated tests")
    config.addinivalue_line("markers", "integration: tests using a temp DB or loopback server")
