from dataclasses import dataclass
from typing import Optional

@dataclass
class BusinessConnection:
    user_id: Optional[int] = None
    user_chat_id: Optional[int] = None