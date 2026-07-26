def test_finance_hub_routes_are_mounted():
    from main import app

    paths = set(app.openapi()["paths"])
    assert "/networth" in paths
    assert "/networth/breakdown" in paths
    assert "/insights/cashflow" in paths
    assert "/goals" in paths
    assert "/rates/crypto" in paths
