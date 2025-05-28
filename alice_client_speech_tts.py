import asyncio
import websockets  # type: ignore
import json
import uuid
import time
from typing import Any, Dict, List, Optional, Set, Tuple, Callable

# ==============================================================================
DEFAULT_SERVER_URL = 'wss://uniproxy.alice.ya.ru/uni.ws'
REQUEST_TIMEOUT_SECONDS = 10
# ==============================================================================
PRESET_OAUTH_TOKEN: str = "y0__xCAQAAAAAxxxxxxxxxxxx_xxxxxxxxX"
# ==============================================================================


# ==============================================================================
# Класс YandexAliceClientError
# ==============================================================================
class YandexAliceClientError(Exception):
    """
    Базовое исключение для ошибок клиента Яндекс Алисы.
    """
    pass


# ==============================================================================
# Класс ActiveRequest
# ==============================================================================
class ActiveRequest:
    """
    Представляет активный запрос, ожидающий ответа от сервера.
    Хранит информацию о необходимых данных и Future для асинхронного ожидания.
    """

    def __init__(self, message_id: str, needs: Set[str]):
        self.message_id = message_id
        self.at = time.time()
        self.needs = needs
        self.directives: List[Dict[str, Any]] = []
        self.audio: Optional[bytes] = None
        self.future = asyncio.Future()

    def resolve(self, result: Any):
        if not self.future.done():
            self.future.set_result(result)

    def reject(self, exception: Exception):
        if not self.future.done():
            self.future.set_exception(exception)


# ==============================================================================
# Класс AudioStream
# ==============================================================================
class AudioStream:
    """
    Представляет аудиопоток, связанный с конкретным запросом (message_id).
    Собирает бинарные чанки аудио.
    """

    def __init__(self, message_id: str, stream_id: int):
        self.message_id = message_id
        self.stream_id = stream_id
        self.buffers: List[bytes] = []


# ==============================================================================
# Класс YandexAliceClient
# ==============================================================================
class YandexAliceClient:
    """
    Асинхронный клиент для взаимодействия с WebSocket-сервисом uniproxy Яндекс Алисы.
    Поддерживает отправку текстовых запросов и запросов на синтез речи (TTS).
    Управляет соединением, обработкой сообщений и активными запросами.
    """

    def __init__(self, server_url: str = DEFAULT_SERVER_URL, oauth_token: Optional[str] = None):
        self.server_url = server_url
        self.oauth_token: Optional[str] = oauth_token
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._active_requests: Dict[str, ActiveRequest] = {}
        self._active_streams: Dict[int, AudioStream] = {}
        self._receive_loop_task: Optional[asyncio.Task] = None
        self._is_connected = False

    def set_oauth_token(self, token: str) -> None:
        self.oauth_token = token if token else None

    async def connect(self) -> None:
        if self._is_connected and self._ws:
            print("Уже подключен.")
            return
        try:
            print(f"Подключение к {self.server_url}...")
            self._ws = await websockets.connect(
                self.server_url,
                ping_interval=10,
                ping_timeout=20
            )
            self._is_connected = True
            self._receive_loop_task = asyncio.create_task(self._receive_loop())
            print("Подключено!")
        except Exception as e:
            self._is_connected = False
            raise YandexAliceClientError(f"Не удалось подключиться к {self.server_url}: {e}")

    async def _receive_loop(self) -> None:
        if not self._ws:
            return
        try:
            async for raw_message in self._ws:
                await self._on_message(raw_message)
        except websockets.exceptions.ConnectionClosed as e:
            print(f"Соединение закрыто: {e}")
            self._is_connected = False
            for req_id, req_obj in list(self._active_requests.items()):
                req_obj.reject(YandexAliceClientError(f"Соединение закрыто во время ожидания ответа на {req_id}"))
                self._active_requests.pop(req_id, None)
        except Exception as e:
            print(f"Ошибка в цикле приема сообщений: {e}")
            self._is_connected = False

    def _get_application_payload(self) -> Dict[str, Any]:
        return {
            "app_id": "aliced_python_speech",
            "app_version": "1.0.0",
            "os_version": "CLI",
            "platform": "python_async_client",
            "uuid": str(uuid.uuid4()),
            "lang": "ru-RU",
            "client_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "timezone": "Europe/Moscow",
            "timestamp": str(int(time.time())),
        }

    async def _send_event(self, namespace: str, name: str, payload: Dict[str, Any]) -> str:
        if not self._ws or not self._is_connected:
            raise YandexAliceClientError("WebSocket не подключен.")

        message_id = str(uuid.uuid4())
        event_data = {
            "header": {
                "namespace": namespace,
                "name": name,
                "messageId": message_id,
            },
            "payload": payload
        }
        try:
            await self._ws.send(json.dumps({"event": event_data}))
            return message_id
        except Exception as e:
            raise YandexAliceClientError(f"Ошибка при отправке события: {e}")

    async def _on_message(self, raw_data: Any) -> None:
        message_json: Optional[Dict[str, Any]] = None
        is_binary = False

        if isinstance(raw_data, str):
            try:
                message_json = json.loads(raw_data)
            except json.JSONDecodeError:
                print(f"Не удалось декодировать JSON из строки: {raw_data[:200]}...")
                return
        elif isinstance(raw_data, bytes):
            is_binary = True
        else:
            print(f"Получен неизвестный тип сообщения: {type(raw_data)}")
            return

        if is_binary and isinstance(raw_data, bytes):
            await self._on_binary_audio(raw_data)
            return

        if message_json:
            if "directive" in message_json:
                await self._on_directive(message_json["directive"])
            elif "streamcontrol" in message_json:
                await self._on_stream_control(message_json["streamcontrol"])
            else:
                print(f"Неизвестный формат JSON сообщения (ключи: {list(message_json.keys())})")

    async def _on_binary_audio(self, data: bytes) -> None:
        if len(data) < 4:
            return
        stream_id = int.from_bytes(data[:4], 'big')
        audio_chunk = data[4:]

        stream = self._active_streams.get(stream_id)
        if not stream:
            return
        stream.buffers.append(audio_chunk)

    async def _on_directive(self, directive: Dict[str, Any]) -> None:
        header = directive.get("header", {})
        ref_message_id = header.get("refMessageId")
        directive_name = header.get("name")

        if not ref_message_id:
            return

        request_obj = self._active_requests.get(ref_message_id)
        if not request_obj:
            return

        request_obj.directives.append(directive)
        if directive_name in request_obj.needs:
            request_obj.needs.remove(directive_name)

        if directive_name == "Speak":
            stream_id = header.get("streamId")
            if stream_id is not None:
                self._active_streams[stream_id] = AudioStream(message_id=ref_message_id, stream_id=stream_id)
                if "audio" in request_obj.needs:
                    request_obj.needs.add("audio_stream_ended")
            else:
                print(f"Директива Speak без streamId для запроса {ref_message_id}.")

        await self._check_request_completion(request_obj)

    async def _on_stream_control(self, stream_control: Dict[str, Any]) -> None:
        stream_id = stream_control.get("streamId")

        if stream_id is None:
            print("Сообщение streamcontrol без streamId.")
            return

        stream_obj = self._active_streams.pop(stream_id, None)
        if not stream_obj:
            return

        request_obj = self._active_requests.get(stream_obj.message_id)
        if not request_obj:
            return

        request_obj.audio = b"".join(stream_obj.buffers)

        if "audio" in request_obj.needs:
            request_obj.needs.remove("audio")
        if "audio_stream_ended" in request_obj.needs:
            request_obj.needs.remove("audio_stream_ended")

        await self._check_request_completion(request_obj)

    async def _check_request_completion(self, request_obj: ActiveRequest) -> None:
        if not request_obj.needs:
            result = {
                "directives": request_obj.directives,
                "audio": request_obj.audio
            }
            request_obj.resolve(result)

    async def _wait_for_response(self, message_id: str, needs: Set[str]) -> Dict[str, Any]:
        request_obj = ActiveRequest(message_id=message_id, needs=needs)
        self._active_requests[message_id] = request_obj

        def _cleanup_request(fut: asyncio.Future):
            self._active_requests.pop(message_id, None)
            streams_to_remove = [sid for sid, stream in self._active_streams.items() if stream.message_id == message_id]
            for sid in streams_to_remove:
                self._active_streams.pop(sid, None)

        request_obj.future.add_done_callback(_cleanup_request)

        try:
            return await asyncio.wait_for(request_obj.future, timeout=REQUEST_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            if not request_obj.future.done():
                request_obj.reject(
                    YandexAliceClientError(f"Таймаут ожидания ответа на запрос {message_id} (ожидались: {needs})"))
            raise

    async def send_text(self, text: str, is_tts_response: bool = False) -> Dict[str, Any]:
        payload = {
            "request": {
                "voice_session": is_tts_response,
                "event": {
                    "type": "text_input",
                    "text": text
                }
            },
            "application": self._get_application_payload()
        }

        if self.oauth_token:
            if 'additional_options' not in payload['request']:
                payload['request']['additional_options'] = {}
            payload['request']['additional_options']['oauth_token'] = self.oauth_token

        message_id = await self._send_event("Vins", "TextInput", payload)

        expected_needs: Set[str] = {"VinsResponse"}
        if is_tts_response:
            expected_needs.add("audio")

        response_data = await self._wait_for_response(message_id, expected_needs)

        final_response_payload = {}
        vins_response_directive = None
        if response_data.get("directives"):
            for directive in response_data["directives"]:
                if directive.get("header", {}).get("name") == "VinsResponse":
                    vins_response_directive = directive
                    final_response_payload = directive.get("payload", {}).get("response", {})
                    break

        if not final_response_payload and response_data.get('error'):
            final_response_payload = {"error": str(response_data['error'])}
        elif not vins_response_directive and not response_data.get('error'):
            final_response_payload = {"error": "Директива VinsResponse не найдена в ответе."}

        return {
            "response_payload": final_response_payload,
            "audio": response_data.get("audio"),
            "full_directives": response_data.get("directives")
        }

    async def tts(self, text: str, voice: str = "shitova.us", lang: str = "ru-RU",
                  format_audio: str = "audio/opus", emotion: str = "neutral",
                  quality: str = "UltraHigh") -> Optional[bytes]:
        if not text: raise ValueError("Текст для TTS не может быть пустым.")

        payload = {
            "voice": voice,
            "lang": lang,
            "format": format_audio,
            "emotion": emotion,
            "quality": quality,
            "text": text
        }

        if self.oauth_token:
            payload["auth_token"] = self.oauth_token

        message_id = await self._send_event("TTS", "Generate", payload)
        response_data = await self._wait_for_response(message_id, {"audio"})
        return response_data.get("audio")

    async def close(self) -> None:
        print("Начало закрытия клиента...")
        if self._receive_loop_task and not self._receive_loop_task.done():
            self._receive_loop_task.cancel()
            try:
                await self._receive_loop_task
            except asyncio.CancelledError:
                print("Цикл приема сообщений отменен.")
            except Exception as e:
                print(f"Исключение при отмене цикла приема: {type(e).__name__} - {e}")
        if self._ws and self._is_connected:
            try:
                print("Закрытие WebSocket соединения...")
                await self._ws.close()
                print("Соединение WebSocket успешно закрыто.")
            except websockets.exceptions.ConnectionClosedOK:
                print("Соединение WebSocket уже было корректно закрыто.")
            except Exception as e:
                print(f"Ошибка при закрытии WebSocket: {type(e).__name__} - {e}")

        self._is_connected = False
        self._ws = None
        self._active_requests.clear()
        self._active_streams.clear()
        print("Клиент полностью остановлен.")


# ==============================================================================
# Вспомогательные функции
# ==============================================================================
def save_audio_to_file(audio_bytes: Optional[bytes], filename: str = "response.opus"):
    """
    Сохраняет байты аудио в файл.
    """
    if not audio_bytes:
        print("Нет аудиоданных для сохранения.")
        return
    try:
        with open(filename, "wb") as f:
            f.write(audio_bytes)
        print(f"Аудио успешно сохранено в файл: {filename}")
    except IOError as e:
        print(f"Ошибка при сохранении файла '{filename}': {e}")


# ==============================================================================
# Основная логика интерактивного цикла для TTS
# ==============================================================================
async def main_interactive_loop():
    print("Чтобы остановить программу, введите 'q', 'quit' или 'exit'.") # Добавлено сообщение о выходе

    active_oauth_token: Optional[str] = PRESET_OAUTH_TOKEN.strip()
    if active_oauth_token:
        print(f"Используется OAuth токен (первые 10 символов): {active_oauth_token[:10]}...")
    else:
        print("Предупреждение: OAuth токен не указан. " #
              "Некоторые функции могут работать некорректно или быть недоступны.") # Изменено предупреждение

    client = YandexAliceClient(oauth_token=active_oauth_token)
    try:
        await client.connect()

        while True:
            try:
                user_text = input("Введите текст для озвучки: ").strip() # Изменен промпт ввода
            except EOFError:
                print("\nВыход (EOF)...");
                break
            except KeyboardInterrupt:
                print("\nВыход (Ctrl+C)...");
                break

            if not user_text:
                continue

            if user_text.lower() in ['q', 'quit', 'exit']:
                print("Выход из программы по команде пользователя.")
                break

            print(f"\n[Алиса TTS] < Отправляю текст '{user_text}' для синтеза речи...")
            try:
                audio_data = await client.tts(user_text)

                if audio_data:
                    print(f"[Алиса TTS] > Получено аудио: {len(audio_data)} байт")
                    timestamp = int(time.time())
                    output_filename = f"tts_output_{timestamp}.opus"
                    save_audio_to_file(audio_data, output_filename)
                else:
                    print("[Алиса TTS] > Не удалось получить аудио.")

            except ValueError as ve:
                print(f"Ошибка ввода: {ve}")
            except YandexAliceClientError as e:
                print(f"Ошибка клиента Алисы при синтезе речи: {e}")
                if "Соединение закрыто" in str(e) or not client._is_connected:
                    print("Попытка переподключения...")
                    try:
                        await client.close()
                        await client.connect()
                        print("Переподключение успешно.")
                    except Exception as recon_e:
                        print(f"Не удалось переподключиться: {recon_e}. Завершение программы.")
                        break
            except Exception as e:
                import traceback
                print(f"Произошла непредвиденная ошибка при синтезе речи: {e}")
                traceback.print_exc()


    except ConnectionRefusedError:
        print(f"Ошибка подключения: сервер {client.server_url} отказал в соединении.")
    except YandexAliceClientError as e:
        print(f"Критическая ошибка клиента Алисы: {e}")
    except KeyboardInterrupt:
        print("\nПрограмма прервана пользователем (Ctrl+C из цикла).")
    except Exception as e:
        import traceback
        print(f"Произошла критическая непредвиденная ошибка в main_interactive_loop: {e}")
        traceback.print_exc()
    finally:
        print("\nЗавершение работы клиента...")
        if client and client._is_connected:
            await client.close()
        else:
            print("Клиент не был подключен или уже закрыт.")


if __name__ == "__main__":
    try:
        asyncio.run(main_interactive_loop())
    except KeyboardInterrupt:
        print("\nПрограмма прервана пользователем (Ctrl+C).")
    except Exception as e:
        import traceback

        print(f"Необработанное исключение в __main__: {e}")
        traceback.print_exc()
    finally:
        print("Программа завершена.")