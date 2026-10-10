"""The advisors. The order here is the order of their topics and of turns in the parliament."""
from personas import bezos, buffett, churchill, frankl, musk
from personas.base import Persona

ALL: list[Persona] = [frankl.PERSONA, musk.PERSONA, buffett.PERSONA, bezos.PERSONA, churchill.PERSONA]
BY_KEY = {p.key: p for p in ALL}
PARLIAMENT = "parliament"
