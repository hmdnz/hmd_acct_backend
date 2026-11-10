"""Matching a bank across providers.

A seller picks their bank once, using Paystack's bank list (Paystack codes such as "058"). Squad pays out with its own
6-digit codes (such as "000013" for GTBank). This finds the likely Squad code(s) for a bank; the caller then proves a
code is right by asking Squad's account lookup and comparing the account holder's name (see service._squad_code_for).
"""
import re

from app.modules.payments.squad_banks import SQUAD_BANKS

# Paystack/CBN code -> Squad code, for the commercial banks. These are the codes Paystack's bank list uses.
# A wrong entry cannot cause a wrong payout: a code is only used after Squad's lookup returns the same account name.
PAYSTACK_TO_SQUAD = {
    "232": "000001",  # Sterling
    "082": "000002",  # Keystone
    "214": "000003",  # FCMB
    "033": "000004",  # UBA
    "063": "000005",  # Access (Diamond)
    "301": "000006",  # Jaiz
    "070": "000007",  # Fidelity
    "076": "000008",  # Polaris
    "050": "000010",  # Ecobank
    "215": "000011",  # Unity
    "221": "000012",  # Stanbic IBTC
    "058": "000013",  # GTBank
    "044": "000014",  # Access
    "057": "000015",  # Zenith
    "011": "000016",  # First Bank
    "035": "000017",  # Wema
    "032": "000018",  # Union
    "030": "000020",  # Heritage
    "068": "000021",  # Standard Chartered
    "100": "000022",  # Suntrust
    "101": "000023",  # Providus
    "102": "000025",  # Titan Trust
    "000026": "000026",  # Taj
    "103": "000027",  # Globus
    "303": "000029",  # Lotus
    "105": "000031",  # Premium Trust
    "107": "000036",  # Optimus
}

_SQUAD_CODES = {code for code, _ in SQUAD_BANKS}
_NOISE = {"bank", "banks", "plc", "ltd", "limited", "microfinance", "mfb", "of", "the", "and", "nigeria", "nig"}


def _words(text: str) -> list[str]:
    return [w for w in re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split() if w not in _NOISE]


def candidates(paystack_code: str, bank_name: str | None) -> list[str]:
    """Squad codes worth trying for this bank, best guess first (at most 4)."""
    found: list[str] = []

    def add(code: str | None) -> None:
        if code and code in _SQUAD_CODES and code not in found:
            found.append(code)

    add(PAYSTACK_TO_SQUAD.get(paystack_code))
    add(paystack_code if len(paystack_code) == 6 else None)  # microfinance banks often share one code
    wanted = _words(bank_name or "")
    if wanted:
        for code, name in SQUAD_BANKS:
            theirs = _words(name)
            if theirs and (theirs == wanted or (len(wanted) >= 2 and set(wanted) <= set(theirs)) or (len(theirs) >= 2 and set(theirs) <= set(wanted))):
                add(code)
    return found[:4]


def same_person(a: str, b: str) -> bool:
    """Do two account names from two banks' lookups name the same holder? Order and punctuation are ignored."""
    x, y = set(_words_keep(a)), set(_words_keep(b))
    return bool(x) and (x == y or (len(x) >= 2 and len(y) >= 2 and (x <= y or y <= x)))


def _words_keep(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split()
