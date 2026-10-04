from dataclasses import asdict, dataclass


@dataclass
class Anime:
    id: str
    title: str
    cover: str = ""
    year: str = ""
    description: str = ""

    def json(self):
        return asdict(self)


@dataclass
class Episode:
    id: str
    number: str
    title: str
    filler: bool = False

    def json(self):
        return asdict(self)


@dataclass
class Source:
    quality: str
    url: str
    language: str = "sub"
    subtitles: list[str] | None = None

    def json(self):
        return asdict(self)
