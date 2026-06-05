"""KYC(고객확인제도) 테스트."""


def _kyc_payload(account_id="ACC-KYC-001"):
    return {
        "account_id": account_id,
        "customer_name": "홍길동",
        "id_type": "RESIDENT_ID",
        "id_number_masked": "900101-1*****",
        "risk_grade": "LOW",
    }


def test_register_kyc(client, staff_auth):
    resp = client.post("/kyc", json=_kyc_payload("ACC-KYC-T01"), headers=staff_auth)
    assert resp.status_code == 201
    body = resp.json()
    assert body["account_id"] == "ACC-KYC-T01"
    assert body["verification_status"] == "PENDING"


def test_register_kyc_unauthenticated(client):
    resp = client.post("/kyc", json=_kyc_payload("ACC-KYC-T02"))
    assert resp.status_code == 401


def test_register_kyc_duplicate(client, staff_auth):
    client.post("/kyc", json=_kyc_payload("ACC-KYC-T03"), headers=staff_auth)
    resp = client.post("/kyc", json=_kyc_payload("ACC-KYC-T03"), headers=staff_auth)
    assert resp.status_code == 400


def test_register_kyc_invalid_id_type(client, staff_auth):
    payload = _kyc_payload("ACC-KYC-T04")
    payload["id_type"] = "DRIVER_LICENSE"
    resp = client.post("/kyc", json=payload, headers=staff_auth)
    assert resp.status_code == 422


def test_register_kyc_invalid_risk_grade(client, staff_auth):
    payload = _kyc_payload("ACC-KYC-T05")
    payload["risk_grade"] = "CRITICAL"
    resp = client.post("/kyc", json=payload, headers=staff_auth)
    assert resp.status_code == 422


def test_get_kyc_as_risk_officer(client, staff_auth, risk_auth):
    client.post("/kyc", json=_kyc_payload("ACC-KYC-T06"), headers=staff_auth)
    resp = client.get("/kyc/ACC-KYC-T06", headers=risk_auth)
    assert resp.status_code == 200
    assert resp.json()["account_id"] == "ACC-KYC-T06"


def test_get_kyc_forbidden_for_staff(client, staff_auth):
    resp = client.get("/kyc/ACC-KYC-T06", headers=staff_auth)
    assert resp.status_code == 403


def test_verify_kyc(client, staff_auth, risk_auth):
    """RISK_OFFICER는 PENDING KYC를 VERIFIED로 승인할 수 있다."""
    client.post("/kyc", json=_kyc_payload("ACC-KYC-T07"), headers=staff_auth)
    resp = client.put("/kyc/ACC-KYC-T07/verify", headers=risk_auth)
    assert resp.status_code == 200
    assert resp.json()["verification_status"] == "VERIFIED"


def test_verify_already_verified_kyc(client, staff_auth, risk_auth):
    client.post("/kyc", json=_kyc_payload("ACC-KYC-T08"), headers=staff_auth)
    client.put("/kyc/ACC-KYC-T08/verify", headers=risk_auth)
    resp = client.put("/kyc/ACC-KYC-T08/verify", headers=risk_auth)
    assert resp.status_code == 400
