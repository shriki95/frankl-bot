from dataclasses import dataclass


@dataclass(frozen=True)
class Persona:
    key: str            # stable id stored in the database
    name: str           # "אילון מאסק"
    short: str          # how others call him: "אילון"
    emoji: str
    domain: str         # one line: what he is good for (menus, parliament roster)
    prompt: str         # who he is and how he talks
    opening: str        # first message in his topic
    memory_focus: str   # what his memory should pay special attention to
    aliases: tuple[str, ...] = ()  # names the user may call him by

    @property
    def title(self):
        return f"{self.emoji} {self.name}"
