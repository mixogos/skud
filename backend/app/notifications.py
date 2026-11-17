"""Рассылка уведомлений подключённым клиентам (лента событий в реальном времени).

Сообщения публикуются из любых потоков (обработчик видео, обработчики запросов)
и доставляются через WebSocket в цикле событий сервера.
"""
import asyncio


class Broadcaster:
    QUEUE_SIZE = 500

    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: set[asyncio.Queue] = set()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def unbind(self) -> None:
        self._loop = None
        self._clients.clear()

    def register(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.QUEUE_SIZE)
        self._clients.add(queue)
        return queue

    def unregister(self, queue: asyncio.Queue) -> None:
        self._clients.discard(queue)

    def publish(self, message_type: str, data: dict) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._fanout, {"type": message_type, "data": data})
        except RuntimeError:
            pass  # сервер останавливается

    def _fanout(self, message: dict) -> None:
        for queue in list(self._clients):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass  # медленный клиент: сообщение пропускается, список он обновит запросом


broadcaster = Broadcaster()
