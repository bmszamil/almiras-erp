"""Review of 838e89b, findings 13 and 15: otp_rejected only on a rejection; personal data in the e-invoice
list."""
from datetime import date

import pytest

from zatca import services
from zatca.client import ApiResponse
from zatca.conftest import einvoice_of, issue, onboard, run
from zatca.messages import ZatcaError
from zatca.models import OnboardingEvent
from zatca.testing import FakeZatcaClient

pytestmark = pytest.mark.django_db
T = FakeZatcaClient


@pytest.fixture
def live(zw):
    zw.egs1 = onboard(zw, zw.tst1, zw.admin1)
    return zw


# --- finding 13: otp_rejected only for a real rejection ---------------------------------------------------------

@pytest.mark.parametrize("response, code", [(ApiResponse(None, {}, "TimeoutError"), "zatca_unreachable"),
                                            (ApiResponse(503, {}), "zatca_unreachable"),
                                            (ApiResponse(200, {"requestID": "1"}), "no_certificate")])
def test_f13_no_answer_is_not_an_otp_rejection(zw, response, code):
    from zatca.conftest import keys

    keys(zw, zw.tst1, zw.admin1)
    services.enable_profile(services.create_profile(zw.tst1, user=zw.admin1, go_live_date=date(2026, 1, 1)),
                            user=zw.admin1)
    egs = services.create_egs_unit(zw.tst1, user=zw.admin1, code="EGS1", name_en="x", name_ar="س",
                                   serial_number="1-T|2-1|3-1", industry="RE")
    services.generate_csr(egs, user=zw.admin1)
    zw.fakes.client.script["compliance_csid"] = [response]
    with pytest.raises(ZatcaError) as exc:
        services.enter_otp(egs, "123456", user=zw.admin1)
    assert exc.value.code == code and exc.value.ar
    assert not OnboardingEvent.objects.filter(egs_unit=egs, action="otp_rejected").exists()
    egs.refresh_from_db()
    assert egs.status == "csr_ready"


# --- finding 15: personal data in the e-invoice list ------------------------------------------------------------

def test_f15_einvoices_withholds_xml_of_individual_buyers(live):
    b2c, b2b = issue(live, live.tst1, simplified=True), issue(live, live.tst1)
    run(live)
    seen = {e.source_document_id: e for e in services.einvoices(live.viewer)}  # zatca.view, no personal data
    assert seen[b2c.pk].signed_xml == "" and seen[b2c.pk].personal_data_withheld
    assert seen[b2b.pk].signed_xml and seen[b2b.pk].cleared_xml and not seen[b2b.pk].personal_data_withheld
    full = {e.source_document_id: e for e in services.einvoices(live.accountant)}  # holds view_personal_data
    assert full[b2c.pk].signed_xml and not full[b2c.pk].personal_data_withheld
    filtered = services.einvoices(live.viewer, entity=live.tst1).filter(pk=einvoice_of(b2c).pk).get()
    assert filtered.signed_xml == ""
    with pytest.raises(ZatcaError):
        services.einvoices(live.viewer).values("signed_xml")
    with pytest.raises(ZatcaError):
        services.einvoices(live.viewer).values_list()
    assert set(services.einvoices(live.viewer).values_list("pk", flat=True)) == {einvoice_of(b2c).pk,
                                                                                einvoice_of(b2b).pk}
    with pytest.raises(ZatcaError):  # the bulk-write guard still holds
        services.einvoices(live.viewer).update(status="pending")
