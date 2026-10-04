from abc import ABC, abstractmethod


class ProviderError(RuntimeError):
    def __init__(self, message: str, kind: str = "provider_unavailable"):
        super().__init__(message)
        self.kind = kind


class Provider(ABC):
    name = "provider"

    @abstractmethod
    def search(self, query: str) -> list[dict]: ...

    @abstractmethod
    def details(self, anime_id: str) -> dict: ...

    @abstractmethod
    def episodes(self, anime_id: str) -> list[dict]: ...

    @abstractmethod
    def sources(self, episode_id: str, language: str = "sub") -> list[dict]: ...
