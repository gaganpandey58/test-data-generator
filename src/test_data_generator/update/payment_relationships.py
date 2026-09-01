"""Source-defined Claim↔Payment matching metadata."""

from typing import Final

PAYMENT_MATCHING_RULES: Final = {
    "payment-professional": {
        "header": (
            "CH_PATIENT_CLIENT_ID",
            "CH_CLAIM_SERVICE_FROM_DATE",
            "CH_CLAIM_SERVICE_TO_DATE",
            "CH_BILLING_PROVIDER_FEDERAL_TAX_ID",
            "CH_BILLING_PROVIDER_NPI",
            "CH_RENDERING_PROVIDER_NPI",
            "CH_PLACE_OF_SERVICE_CODE",
            "CH_DIAGNOSIS_CODE_01",
            "CH_SUBSCRIBER_CLIENT_ID",
            "CH_CLAIM_FREQUENCY_CODE",
            "CH_CHARGE_AMOUNT",
            "CH_PATIENT_ACCOUNT_CONTROL_NUMBER",
        ),
        "line": (
            "CD_SERVICE_FROM_DATE",
            "CD_SERVICE_TO_DATE",
            "CD_SUBMITTED_PROCEDURE_CODE_QUALIFIER",
            "CD_SUBMITTED_PROCEDURE_CODE",
            "CD_SUBMITTED_PROCEDURE_MODIFIER_01",
            "CD_SUBMITTED_PROCEDURE_MODIFIER_02",
            "CD_SUBMITTED_PROCEDURE_MODIFIER_03",
            "CD_SUBMITTED_PROCEDURE_MODIFIER_04",
            "CD_CHARGE_AMOUNT",
        ),
        "dates": ("CH_CLAIM_PAID_DATE", "CD_LINE_PAID_DATE"),
    },
    "payment-institutional": {
        "header": (
            "CH_PATIENT_CLIENT_ID",
            "CH_CLAIM_SERVICE_FROM_DATE",
            "CH_CLAIM_SERVICE_TO_DATE",
            "CH_BILLING_PROVIDER_FEDERAL_TAX_ID",
            "CH_BILLING_PROVIDER_NPI",
            "CH_RENDERING_PROVIDER_NPI",
            "CH_DIAGNOSIS_CODE_01",
            "CH_TYPE_OF_BILL_CODE",
            "CH_SUBSCRIBER_CLIENT_ID",
            "CH_CLAIM_FREQUENCY_CODE",
            "CH_CHARGE_AMOUNT",
            "CH_PATIENT_ACCOUNT_CONTROL_NUMBER",
        ),
        "line": (
            "CD_SERVICE_FROM_DATE",
            "CD_SERVICE_TO_DATE",
            "CD_SUBMITTED_PROCEDURE_CODE_QUALIFIER",
            "CD_SUBMITTED_PROCEDURE_CODE",
            "CD_SUBMITTED_PROCEDURE_MODIFIER_01",
            "CD_SUBMITTED_PROCEDURE_MODIFIER_02",
            "CD_SUBMITTED_PROCEDURE_MODIFIER_03",
            "CD_SUBMITTED_PROCEDURE_MODIFIER_04",
            "CD_SUBMITTED_REVENUE_CODE",
            "CD_CHARGE_AMOUNT",
        ),
        "dates": ("CH_CLAIM_PAID_DATE", "CD_LINE_PAID_DATE"),
    },
}
