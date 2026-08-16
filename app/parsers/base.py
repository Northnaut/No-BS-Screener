from dataclasses import dataclass
from datetime import datetime


@dataclass
class FetchedPost:
    external_id: str
    title: str
    text: str
    url: str
    published_at: datetime
