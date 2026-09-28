"""Shopify and WordPress adapters for the Platform Purge Console."""
import json

import httpx

from botpurge.purge.adapters import ShopifyAdapter, WordPressAdapter, build


def test_shopify_import_and_tags(client):
    calls = []

    def handler(req: httpx.Request):
        assert req.headers["X-Shopify-Access-Token"] == "shpat_x" and req.url.host == "demo.myshopify.com"
        body = json.loads(req.content)
        calls.append(body)
        q = body["query"]
        if "customers(" in q:
            if body["variables"]["after"] is None:
                return httpx.Response(200, json={"data": {"customers": {"pageInfo": {"hasNextPage": True, "endCursor": "c1"}, "nodes": [
                    {"id": "gid://shopify/Customer/1", "email": "real@shop.com", "firstName": "Ana", "lastName": "Ruiz",
                     "createdAt": "2024-01-01T00:00:00Z", "verifiedEmail": True, "tags": [], "numberOfOrders": "3", "amountSpent": {"amount": "120.0"}}]}}})
            return httpx.Response(200, json={"data": {"customers": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [
                {"id": "gid://shopify/Customer/2", "email": "xk2j9@mail.ru", "firstName": None, "lastName": None,
                 "createdAt": "2026-09-01T00:00:00Z", "verifiedEmail": False, "tags": [], "numberOfOrders": "0", "amountSpent": {"amount": "0.0"}}]}}})
        if "customerDelete" in q:
            return httpx.Response(200, json={"data": {"customerDelete": {"userErrors": [{"message": "Customer can't be deleted because they have orders"}]}}})
        key = "tagsAdd" if "tagsAdd" in q else "tagsRemove"
        return httpx.Response(200, json={"data": {key: {"userErrors": []}}})

    s = ShopifyAdapter("https://demo.myshopify.com/", "shpat_x", http=httpx.Client(transport=httpx.MockTransport(handler)))
    accs = s.fetch_accounts()
    assert [a.account_id for a in accs] == ["1", "2"] and accs[0].tags == ["paying"] and accs[1].email_verified is False
    s.enforce("2", "suspended")
    assert calls[-2]["variables"] == {"id": "gid://shopify/Customer/2", "tags": ["botpurge-challenged", "botpurge-restricted"]}
    assert calls[-1]["variables"]["tags"] == ["botpurge-suspended"]
    s.enforce("2", "removed")                       # can't delete: stays tagged suspended
    assert "customerDelete" in calls[-2]["query"] and calls[-1]["variables"]["tags"] == ["botpurge-suspended"]
    s.enforce("2", "active")
    assert "tagsRemove" in calls[-1]["query"] and len(calls[-1]["variables"]["tags"]) == 3
    # Paying customers are exempt from purges by default.
    key = client.post("/api/purge/tenants", json={"name": "Shop"}).json()["api_key"]
    assert client.put("/api/purge/adapter", json={"kind": "shopify", "secret": "t"}, headers={"X-API-Key": key}).status_code == 400
    assert client.put("/api/purge/adapter", json={"kind": "shopify", "secret": "t", "shop": "demo"}, headers={"X-API-Key": key}).status_code == 200


def test_wordpress_import_roles_and_delete():
    calls = []

    def handler(req: httpx.Request):
        assert req.headers["authorization"].startswith("Basic ")
        calls.append((req.method, req.url.path, dict(req.url.params), json.loads(req.content) if req.content else None))
        if req.method == "GET":
            page = req.url.params["page"]
            users = [{"id": 1, "username": "admin", "name": "Site Admin", "email": "a@x.com", "registered_date": "2020-01-01T00:00:00", "roles": ["administrator"]}] \
                if page == "1" else [{"id": 7, "username": "buyviagra77", "name": "buyviagra77", "email": "b@x.ru", "registered_date": "2026-09-01T00:00:00", "roles": ["customer"]}]
            return httpx.Response(200, json=users, headers={"X-WP-TotalPages": "2"})
        return httpx.Response(200, json={})

    w = build("wordpress", {"base_url": "https://blog.example", "username": "admin", "member_role": "customer"}, "abcd efgh ijkl",
              http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert isinstance(w, WordPressAdapter)
    accs = w.fetch_accounts()
    assert [(a.account_id, a.tags) for a in accs] == [("1", ["staff"]), ("7", [])]
    w.enforce("7", "restricted")
    assert calls[-1][:2] == ("POST", "/wp-json/wp/v2/users/7") and calls[-1][3] == {"roles": []}
    w.enforce("7", "active")
    assert calls[-1][3] == {"roles": ["customer"]}
    try:
        w.enforce("7", "removed")
        raise AssertionError("removal must need a user to reassign content to")
    except Exception as exc:
        assert "reassign_to" in str(exc)
    w.reassign = "1"
    w.enforce("7", "removed")
    assert calls[-1][:3] == ("DELETE", "/wp-json/wp/v2/users/7", {"force": "true", "reassign": "1"})
