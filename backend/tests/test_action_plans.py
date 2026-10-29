
import pytest
from httpx import AsyncClient


PLAN = "Cortar delivery para 2x no mes e pagar a fatura antes do vencimento."


@pytest.mark.asyncio
async def test_create_and_list(client: AsyncClient, auth_headers):
    r = await client.post(
        "/api/action-plans", headers=auth_headers,
        json={"month": "2026-11-01", "body": PLAN},
    )
    assert r.status_code == 201
    created = r.json()
    assert created["body"] == PLAN
    assert created["review"] is None
    assert created["reviewed_at"] is None

    listed = (await client.get("/api/action-plans", headers=auth_headers)).json()
    assert [p["id"] for p in listed] == [created["id"]]


@pytest.mark.asyncio
async def test_month_is_snapped_to_the_first(client: AsyncClient, auth_headers):
    """A plan is for a month. Without snapping, two plans for the same month
    written on different days would both slip past the unique constraint."""
    r = await client.post(
        "/api/action-plans", headers=auth_headers,
        json={"month": "2026-12-17", "body": PLAN},
    )
    assert r.status_code == 201
    assert r.json()["month"] == "2026-12-01"


@pytest.mark.asyncio
async def test_second_plan_for_the_same_month_is_refused(client: AsyncClient, auth_headers):
    first = await client.post(
        "/api/action-plans", headers=auth_headers,
        json={"month": "2027-01-01", "body": PLAN},
    )
    assert first.status_code == 201
    # Same month, different day: still the same month.
    clash = await client.post(
        "/api/action-plans", headers=auth_headers,
        json={"month": "2027-01-28", "body": "outro plano"},
    )
    assert clash.status_code == 409


@pytest.mark.asyncio
async def test_empty_plan_is_refused(client: AsyncClient, auth_headers):
    r = await client.post(
        "/api/action-plans", headers=auth_headers,
        json={"month": "2027-02-01", "body": "   "},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_review_marks_the_plan_and_clearing_it_reopens(client: AsyncClient, auth_headers):
    """`review` is the only record that a plan was judged, so the timestamp
    has to follow it in both directions — otherwise a cleared verdict leaves
    a row still claiming it was reviewed."""
    created = (await client.post(
        "/api/action-plans", headers=auth_headers,
        json={"month": "2027-03-01", "body": PLAN},
    )).json()

    judged = (await client.patch(
        f"/api/action-plans/{created['id']}", headers=auth_headers,
        json={"review": "Cumpriu o delivery, falhou na fatura."},
    )).json()
    assert judged["review"].startswith("Cumpriu")
    assert judged["reviewed_at"] is not None

    reopened = (await client.patch(
        f"/api/action-plans/{created['id']}", headers=auth_headers,
        json={"review": ""},
    )).json()
    assert reopened["review"] is None
    assert reopened["reviewed_at"] is None


@pytest.mark.asyncio
async def test_filter_by_month(client: AsyncClient, auth_headers):
    await client.post("/api/action-plans", headers=auth_headers,
                      json={"month": "2027-04-01", "body": "abril"})
    await client.post("/api/action-plans", headers=auth_headers,
                      json={"month": "2027-05-01", "body": "maio"})

    only_may = (await client.get(
        "/api/action-plans", headers=auth_headers, params={"month": "2027-05-09"},
    )).json()
    assert [p["body"] for p in only_may] == ["maio"]


@pytest.mark.asyncio
async def test_unknown_plan_is_404(client: AsyncClient, auth_headers):
    missing = "00000000-0000-0000-0000-000000000000"
    r = await client.patch(f"/api/action-plans/{missing}", headers=auth_headers,
                           json={"body": "x"})
    assert r.status_code == 404
    assert (await client.delete(f"/api/action-plans/{missing}", headers=auth_headers)).status_code == 404


@pytest.mark.asyncio
async def test_delete(client: AsyncClient, auth_headers):
    created = (await client.post(
        "/api/action-plans", headers=auth_headers,
        json={"month": "2027-06-01", "body": PLAN},
    )).json()
    assert (await client.delete(f"/api/action-plans/{created['id']}", headers=auth_headers)).status_code == 204
    assert (await client.get("/api/action-plans", headers=auth_headers,
                             params={"month": "2027-06-01"})).json() == []
