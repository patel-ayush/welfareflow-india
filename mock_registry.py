"""
Sandboxed mock government registries pre-populated with synthetic citizen records.

These replace live DigiLocker / UIDAI / NPCI production endpoints for the hackathon
demo environment.  All Aadhaar numbers are fictitious and follow no real pattern.
"""
from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Synthetic citizen master records
# Keys intentionally include spelling variants to exercise fuzzy matching.
# ---------------------------------------------------------------------------
MOCK_CITIZEN_DB: Final[dict[str, dict[str, object]]] = {
    "CITIZEN-001": {
        "citizen_id": "CITIZEN-001",
        "full_name_aadhaar": "Ramesh Kumar",
        "full_name_ration_card": "Ramesha K",       # deliberate mismatch
        "full_name_passbook": "R. Kumar",
        "aadhaar_last4": "5678",
        "phone": "9876543210",
        "state": "Karnataka",
        "district": "Mandya",
        "land_area_acres": 2.0,
        "annual_income_inr": 48000,
        "age": 52,
        "occupation": "Farmer",
        "bank_account": "11223344556",
        "bank_ifsc": "SBIN0001234",
        "is_govt_employee": False,
        "existing_health_coverage": None,
    },
    "CITIZEN-002": {
        "citizen_id": "CITIZEN-002",
        "full_name_aadhaar": "Lakshmi Devi",
        "full_name_ration_card": "Laxmi Devi",      # vowel variant (the tested case)
        # Passbook keeps the FULL surname: CITIZEN-002 is the canonical "legit
        # vowel variant passes" demo (see TESTING.md). A truncated "Lakshmi D"
        # here would wrongly flag on the passbook pair and defeat that scenario;
        # truncation-flagging is already covered by CITIZEN-001 and CITIZEN-005.
        "full_name_passbook": "Lakshmi Devi",
        "aadhaar_last4": "9012",
        "phone": "8765432109",
        "state": "Rajasthan",
        "district": "Sikar",
        "land_area_acres": 1.5,
        "annual_income_inr": 36000,
        "age": 44,
        "occupation": "Farmer",
        "bank_account": "22334455667",
        "bank_ifsc": "PUNB0002345",
        "is_govt_employee": False,
        "existing_health_coverage": None,
    },
    "CITIZEN-003": {
        "citizen_id": "CITIZEN-003",
        "full_name_aadhaar": "Suresh Prasad",
        "full_name_ration_card": "Suresh Prasad",   # perfect match
        "full_name_passbook": "Suresh Prasad",
        "aadhaar_last4": "3456",
        "phone": "7654321098",
        "state": "Uttar Pradesh",
        "district": "Varanasi",
        "land_area_acres": 0.8,
        "annual_income_inr": 29000,
        "age": 60,
        "occupation": "Farmer",
        "bank_account": "33445566778",
        "bank_ifsc": "UBIN0003456",
        "is_govt_employee": False,
        "existing_health_coverage": None,
    },
    "CITIZEN-004": {
        "citizen_id": "CITIZEN-004",
        "full_name_aadhaar": "Priya Sharma",
        "full_name_ration_card": "Priya Sharmaa",   # extra vowel
        "full_name_passbook": "Priya Sharma",
        "aadhaar_last4": "7890",
        "phone": "6543210987",
        "state": "Maharashtra",
        "district": "Nashik",
        "land_area_acres": 3.2,
        "annual_income_inr": 75000,
        "age": 38,
        "occupation": "Farmer",
        "bank_account": "44556677889",
        "bank_ifsc": "MAHB0004567",
        "is_govt_employee": False,
        "existing_health_coverage": None,
    },
    "CITIZEN-005": {
        "citizen_id": "CITIZEN-005",
        "full_name_aadhaar": "Mohammed Rashid",
        "full_name_ration_card": "Mohd Rashid",     # abbreviation
        "full_name_passbook": "Mohamed Rashid",
        "aadhaar_last4": "2345",
        "phone": "5432109876",
        "state": "Telangana",
        "district": "Nizamabad",
        "land_area_acres": 1.0,
        "annual_income_inr": 32000,
        "age": 48,
        "occupation": "Farmer",
        "bank_account": "55667788990",
        "bank_ifsc": "ANDB0005678",
        "is_govt_employee": False,
        "existing_health_coverage": None,
    },
}

# ---------------------------------------------------------------------------
# PM-Kisan eligibility rules (Government of India, Scheme 2024 parameters)
# ---------------------------------------------------------------------------
PMKISAN_ELIGIBILITY_RULES: Final[dict[str, object]] = {
    "max_land_acres": 5.0,           # marginal & small farmers
    "excluded_occupations": ["government_employee", "income_tax_payer", "professional"],
    "min_annual_benefit_inr": 6000,
    "installments_per_year": 3,
    "installment_amount_inr": 2000,
    "eligible_states": "all",        # nationwide scheme
}

# ---------------------------------------------------------------------------
# Ayushman Bharat – PM-JAY eligibility rules (2024 parameters)
# ---------------------------------------------------------------------------
AYUSHMAN_ELIGIBILITY_RULES: Final[dict[str, object]] = {
    "max_annual_income_inr": 250000,
    "coverage_amount_inr": 500000,
    "excluded_if_cghs": True,
    "excluded_occupations": ["government_employee"],
    "min_household_size": 1,
    "eligible_states": "all",
}

# ---------------------------------------------------------------------------
# Mock NPCI Aadhaar → Bank Account mapper (last4 is the mock lookup key)
# ---------------------------------------------------------------------------
MOCK_NPCI_DB: Final[dict[str, dict[str, object]]] = {
    "5678": {
        "aadhaar_last4": "5678",
        "bank_account": "11223344556",
        "bank_ifsc": "SBIN0001234",
        "bank_name": "State Bank of India",
        "seeding_status": "SEEDED",
        "npci_ref": "NPCI-REF-2024-001",
    },
    "9012": {
        "aadhaar_last4": "9012",
        "bank_account": "22334455667",
        "bank_ifsc": "PUNB0002345",
        "bank_name": "Punjab National Bank",
        "seeding_status": "SEEDED",
        "npci_ref": "NPCI-REF-2024-002",
    },
    "3456": {
        "aadhaar_last4": "3456",
        "bank_account": "33445566778",
        "bank_ifsc": "UBIN0003456",
        "bank_name": "Union Bank of India",
        "seeding_status": "PENDING",         # deliberate pending state
        "npci_ref": None,
    },
    "7890": {
        "aadhaar_last4": "7890",
        "bank_account": "44556677889",
        "bank_ifsc": "MAHB0004567",
        "bank_name": "Bank of Maharashtra",
        "seeding_status": "SEEDED",
        "npci_ref": "NPCI-REF-2024-004",
    },
    "2345": {
        "aadhaar_last4": "2345",
        "bank_account": "55667788990",
        "bank_ifsc": "ANDB0005678",
        "bank_name": "Andhra Bank",
        "seeding_status": "SEEDED",
        "npci_ref": "NPCI-REF-2024-005",
    },
}
