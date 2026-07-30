"""In-process redirection of superseded Telegram progress message handles."""

from __future__ import annotations

from collections import OrderedDict


class TelegramProgressRedirects:
    """Resolve stale progress targets to the latest writable Telegram message.

    Input presentation relocation and explicit ``/send`` may replace the public
    status message while a committed batch still contains older response-route
    metadata.  Redirects keep those immutable callbacks useful without making a
    superseded Telegram message writable again.
    """

    def __init__(self, *, maximum_entries: int = 2048) -> None:
        if maximum_entries < 1:
            raise ValueError("maximum_entries must be positive")
        self.maximum_entries = maximum_entries
        self._targets: OrderedDict[tuple[int, int], int] = OrderedDict()

    def register(self, *, chat_id: int, old_message_id: int, new_message_id: int) -> None:
        old_key = (int(chat_id), int(old_message_id))
        new_value = int(new_message_id)
        if old_key[1] == new_value:
            return
        # Resolve the destination first so a chain is always acyclic and short.
        destination = self.resolve(chat_id=old_key[0], message_id=new_value)
        if destination == old_key[1]:
            raise ValueError("progress redirect would create a cycle")
        self._targets[old_key] = destination
        self._targets.move_to_end(old_key)
        while len(self._targets) > self.maximum_entries:
            self._targets.popitem(last=False)

    def resolve(self, *, chat_id: int, message_id: int) -> int:
        chat = int(chat_id)
        current = int(message_id)
        visited: list[tuple[int, int]] = []
        seen: set[int] = set()
        for _ in range(64):
            if current in seen:
                break
            seen.add(current)
            key = (chat, current)
            target = self._targets.get(key)
            if target is None:
                break
            visited.append(key)
            current = int(target)
        for key in visited:
            self._targets[key] = current
            self._targets.move_to_end(key)
        return current

    def discard(self, *, chat_id: int, message_id: int) -> None:
        self._targets.pop((int(chat_id), int(message_id)), None)

    def __len__(self) -> int:
        return len(self._targets)
