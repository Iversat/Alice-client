import websocket  # pip install websocket-client
import uuid
import json
import threading
import time
import datetime  # Глобальный импорт модуля datetime
from typing import Dict, Any, Set, List, Optional, Callable, Union


# ==============================================================================
# Класс UniproxyPython (Управление WebSocket и низкоуровневыми сообщениями)
# ==============================================================================
class UniproxyPython:
    """
    Низкоуровневый клиент для WebSocket-сервиса uniproxy.
    Управляет соединением, отправкой/получением сообщений JSON и бинарных данных (аудио).
    """

    def __init__(self):
        self.ws: Optional[websocket.WebSocketApp] = None
        # Словарь для отслеживания активных запросов
        # message_id -> {'event': threading.Event(), 'data': Dict[str, Any], 'needs': Set[str], 'timestamp': float}
        self.requests: Dict[str, Dict[str, Any]] = {}
        # Словарь для отслеживания активных аудиопотоков
        # stream_id  -> {'message_id': str, 'buffers': List[bytes], 'stream_id': int}
        self.streams: Dict[int, Dict[str, Any]] = {}
        self.ws_thread: Optional[threading.Thread] = None
        self.is_connected_event = threading.Event()  # Событие для сигнализации об успешном соединении

    def connect(self, server_url: str, connection_timeout: float = 10.0) -> None:
        """
        Устанавливает WebSocket-соединение с указанным сервером.
        """
        self.is_connected_event.clear()
        self.ws = websocket.WebSocketApp(server_url,
                                         on_open=self._on_open,
                                         on_message=self._on_message,
                                         on_error=self._on_error,
                                         on_close=self._on_close)
        self.ws_thread = threading.Thread(target=self.ws.run_forever, name="UniproxyWSThread")
        self.ws_thread.daemon = True  # Поток завершится при выходе из основной программы
        self.ws_thread.start()

        if not self.is_connected_event.wait(timeout=connection_timeout):
            if self.ws:
                try:
                    self.ws.close()  # Попытка закрыть сокет, если соединение не удалось
                except Exception:
                    pass  # Игнорируем ошибки при закрытии в этом случае
            raise ConnectionError(f"Не удалось подключиться к {server_url} в течение {connection_timeout} секунд.")

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        """Обработчик открытия WebSocket-соединения."""
        self.is_connected_event.set()

    def _on_error(self, ws: websocket.WebSocketApp, error: Exception) -> None:
        """Обработчик ошибок WebSocket."""
        print(f"Ошибка WebSocket: {error}")
        # Завершаем все ожидающие запросы с ошибкой
        for message_id, req_info in list(self.requests.items()):
            if req_info and not req_info['event'].is_set():  # Добавлена проверка req_info
                req_info['data']['error'] = str(error)
                req_info['event'].set()
        self.is_connected_event.clear()  # Сигнализируем, что соединение больше неактивно

    def _on_close(self, ws: websocket.WebSocketApp, close_status_code: Optional[int], close_msg: Optional[str]) -> None:
        """Обработчик закрытия WebSocket-соединения."""
        # print(f"WebSocket соединение закрыто: статус {close_status_code}, сообщение: {close_msg}") # Для отладки
        # Завершаем все ожидающие запросы, если они еще не завершены
        for message_id, req_info in list(self.requests.items()):
            if req_info and not req_info['event'].is_set():  # Добавлена проверка req_info
                req_info['data']['error'] = "Соединение закрыто"
                req_info['event'].set()
        self.is_connected_event.clear()

    def _on_buffer(self, data_buffer: bytes) -> None:
        """Обработчик входящих бинарных данных (частей аудиопотока)."""
        if len(data_buffer) < 4:
            # print(f"Получен слишком короткий бинарный пакет данных: {len(data_buffer)} байт.") # Для отладки
            return

        stream_id = int.from_bytes(data_buffer[:4], byteorder='big')
        stream = self.streams.get(stream_id)

        if not stream:
            # print(f"Предупреждение: Не найден активный поток с ID {stream_id} для бинарных данных.") # Для отладки
            return
        stream['buffers'].append(data_buffer[4:])

    def _on_message(self, ws: websocket.WebSocketApp, message: Union[str, bytes]) -> None:
        """Обработчик входящих сообщений (JSON или бинарных)."""
        if isinstance(message, bytes):
            self._on_buffer(message)
            return

        try:
            response_data = json.loads(message)
        except json.JSONDecodeError:
            # print(f"Ошибка декодирования JSON: {message}") # Для отладки
            return

        request_info: Optional[Dict[str, Any]] = None
        if 'directive' in response_data:
            request_info = self._on_directive(response_data['directive'])
        elif 'streamcontrol' in response_data:
            request_info = self._on_streamcontrol(response_data['streamcontrol'])
        # else:
        # print(f"Неизвестная структура сообщения: {response_data}") # Для отладки

        # Если все "потребности" запроса удовлетворены, сигнализируем об этом
        if request_info and not request_info['needs'] and not request_info['event'].is_set():
            request_info['event'].set()

    def _on_directive(self, directive: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Обрабатывает входящую директиву."""
        header = directive.get('header', {})
        ref_message_id = header.get('refMessageId')
        if not ref_message_id: return None

        request_info = self.requests.get(ref_message_id)
        if not request_info: return None

        if 'directives' not in request_info['data']: request_info['data']['directives'] = []
        request_info['data']['directives'].append(directive)

        directive_name = header.get('name')
        if directive_name in request_info['needs']: request_info['needs'].remove(directive_name)

        if directive_name == 'Speak' and 'streamId' in header:
            self.streams[header['streamId']] = {
                'message_id': ref_message_id,
                'buffers': [],
                'stream_id': header['streamId']
            }
        return request_info

    def _on_streamcontrol(self, streamcontrol: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Обрабатывает сообщение streamcontrol, сигнализирующее о завершении аудиопотока."""
        stream_id = streamcontrol.get('streamId')
        if stream_id is None: return None

        stream_data = self.streams.pop(stream_id, None)
        if not stream_data: return None

        request_info = self.requests.get(stream_data['message_id'])
        if not request_info: return None

        request_info['data']['audio'] = b"".join(stream_data['buffers'])
        if 'audio' in request_info['needs']: request_info['needs'].remove('audio')
        return request_info

    def send_event(self, namespace: str, name: str, payload: Dict[str, Any],
                   header_extra: Optional[Dict[str, Any]] = None) -> str:
        """
        Отправляет событие на WebSocket-сервер.
        Возвращает messageId отправленного события.
        """
        if not self.is_connected_event.is_set() or not self.ws:
            raise ConnectionError("WebSocket не подключен.")

        message_id = str(uuid.uuid4())
        event_header = {"namespace": namespace, "name": name, "messageId": message_id}
        if header_extra: event_header.update(header_extra)

        event_message = {"event": {"header": event_header, "payload": payload}}
        self.ws.send(json.dumps(event_message))
        return message_id

    def receive_data(self, message_id: str, needs_list: List[str], timeout: float = 10.0) -> Dict[str, Any]:
        """
        Ожидает получения данных (директив, аудио) для указанного message_id.
        """
        event = threading.Event()
        self.requests[message_id] = {
            'id': message_id, 'event': event, 'data': {'directives': [], 'audio': None},
            'needs': set(needs_list), 'timestamp': time.time()
        }

        if event.wait(timeout=timeout):
            request_data_snapshot = self.requests.pop(message_id)['data']
            if 'error' in request_data_snapshot:
                raise RuntimeError(f"Ошибка во время выполнения запроса {message_id}: {request_data_snapshot['error']}")
            return request_data_snapshot
        else:
            self.requests.pop(message_id, None)  # Очищаем информацию о запросе при таймауте
            raise TimeoutError(f"Таймаут ожидания ответа для messageId {message_id} (ожидались: {needs_list})")

    def close(self) -> None:
        """Закрывает WebSocket-соединение."""
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass  # Игнорируем ошибки, если сокет уже закрыт или в плохом состоянии
        if self.ws_thread and self.ws_thread.is_alive():
            self.ws_thread.join(timeout=1.0)  # Уменьшенный таймаут для более быстрого выхода


# ==============================================================================
# Класс YandexAliceClientPython (Высокоуровневый API для Алисы)
# ==============================================================================
class YandexAliceClientPython:
    DEFAULT_SERVER_URL = 'wss://uniproxy.alice.ya.ru/uni.ws'

    def __init__(self, server_url: Optional[str] = None):
        self.uniproxy = UniproxyPython()  # Используем класс, определенный выше
        self.server_url: str = server_url or self.DEFAULT_SERVER_URL
        self.client_app_uuid: str = str(uuid.uuid4())
        self.device_uuid: str = str(uuid.uuid4())  # Для synchronize_state

    def connect(self) -> None:
        """Устанавливает WebSocket-соединение с сервером uniproxy."""
        self.uniproxy.connect(self.server_url)

    def synchronize_state(self, auth_token: str, lang: str = 'ru-RU', voice: str = 'levitan') -> str:
        """Отправляет событие SynchronizeState."""
        payload = {"auth_token": auth_token, "uuid": self.device_uuid, "lang": lang, "voice": voice}
        message_id = self.uniproxy.send_event('System', 'SynchronizeState', payload)
        return message_id

    def send_text(self, text: str, is_tts: bool = False) -> Dict[str, Any]:
        """Отправляет текстовый запрос Алисе и получает ответ."""
        if not text: raise ValueError("Текст запроса не может быть пустым.")
        request_payload = {
            "request": {"voice_session": is_tts, "event": {"type": "text_input", "text": text}},
            "application": self._get_application_details()
        }
        message_id = self.uniproxy.send_event('Vins', 'TextInput', request_payload)
        needs = ['VinsResponse']
        if is_tts: needs.append('audio')

        try:
            response_data = self.uniproxy.receive_data(message_id, needs)
        except TimeoutError:
            return {"response": {"error": "Таймаут получения ответа"}, "audio": None}
        except RuntimeError as e:  # Ошибки от Uniproxy (например, ошибка соединения)
            return {"response": {"error": str(e)}, "audio": None}

        vins_response_payload = None
        if response_data.get('directives'):
            for directive in response_data['directives']:
                if directive.get('header', {}).get('name') == 'VinsResponse':
                    vins_response_payload = directive.get('payload', {}).get('response')
                    break

        if vins_response_payload is None:  # Если VinsResponse не найден
            # Проверяем, не было ли ошибки на уровне всего запроса (например, от Uniproxy)
            if response_data.get('error'):
                vins_response_payload = {"error": response_data['error']}
            else:  # Если просто нет VinsResponse директивы
                vins_response_payload = {"error": "Директива VinsResponse не найдена в полученном ответе."}

        return {"response": vins_response_payload, "audio": response_data.get('audio')}

    def tts(self, text: str, voice: str = 'shitova.us', lang: str = 'ru-RU',
            audio_format: str = 'audio/opus', emotion: str = 'neutral',
            quality: str = 'UltraHigh') -> Optional[bytes]:
        """Генерирует TTS аудио для указанного текста."""
        if not text: raise ValueError("Текст для TTS не может быть пустым.")
        payload = {
            "voice": voice, "lang": lang, "format": audio_format,
            "emotion": emotion, "quality": quality, "text": text
        }
        message_id = self.uniproxy.send_event('TTS', 'Generate', payload)
        try:
            response_data = self.uniproxy.receive_data(message_id, ['audio'])
        except TimeoutError:
            return None
        except RuntimeError:  # Ошибки от Uniproxy
            return None
        return response_data.get('audio')

    def _get_application_details(self) -> Dict[str, str]:
        """Генерирует стандартные детали приложения для запросов."""
        return {
            "app_id": "aliced_python_terminal", "app_version": "1.0.2", "os_version": "CLI",
            "platform": "python_terminal", "uuid": self.client_app_uuid, "lang": "ru-RU",
            "client_time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            # Корректное использование datetime
            "timezone": "Europe/Moscow", "timestamp": str(int(time.time()))
        }

    def close(self) -> None:
        """Закрывает WebSocket-соединение."""
        self.uniproxy.close()


# ==============================================================================
# Основная логика терминального чата
# ==============================================================================
def main_terminal_chat():
    client = YandexAliceClientPython()
    is_client_connected = False

    try:
        print("Подключение к Яндекс Алисе...")
        client.connect()
        is_client_connected = True
        print("Подключено! Введите 'exit' или 'quit', чтобы выйти.")
        # print("Для озвучивания последнего ответа Алисы введите /tts.") # <--- ЭТА СТРОКА УДАЛЕНА
        print("----------------------------------------------------")

        last_alice_response_text_for_tts = None

        while True:
            try:
                user_input = input("[Вы] > ")
            except EOFError:  # Обработка Ctrl+D
                print("\nВыход (EOF)...")
                break
            except KeyboardInterrupt:  # Обработка Ctrl+C внутри цикла ввода
                print("\nВыход по команде пользователя (Ctrl+C)...")
                break

            if user_input.lower() in ['exit', 'quit']:
                break

            if user_input.lower() == '/tts':
                if last_alice_response_text_for_tts:
                    print(f"Запрос TTS для фразы: \"{last_alice_response_text_for_tts}\"")
                    audio_bytes = client.tts(last_alice_response_text_for_tts)
                    if audio_bytes:
                        filename = "alice_tts_response.opus"
                        try:
                            with open(filename, 'wb') as f:
                                f.write(audio_bytes)
                            print(f"Аудио сохранено в {filename}")
                        except IOError as e:
                            print(f"Ошибка сохранения аудиофайла: {e}")
                    else:
                        print("Не удалось сгенерировать аудио (возможно, ошибка TTS или пустой ответ).")
                else:
                    print("Нет последней фразы Алисы для озвучивания. Сначала отправьте сообщение Алисе.")
                continue

            if not user_input.strip():  # Пропустить пустой ввод
                continue

            response_data = client.send_text(user_input, is_tts=False)
            alice_response_payload = response_data.get('response')
            current_alice_text_output = None

            if isinstance(alice_response_payload, dict):
                if 'error' in alice_response_payload:  # Ошибка от нашего клиента или от Алисы, переданная через поле error
                    current_alice_text_output = f"(Ошибка: {alice_response_payload['error']})"
                elif 'card' in alice_response_payload and isinstance(alice_response_payload['card'], dict) and 'text' in \
                        alice_response_payload['card']:
                    current_alice_text_output = alice_response_payload['card']['text']
                elif 'text' in alice_response_payload:  # Прямой текст в ответе
                    current_alice_text_output = alice_response_payload['text']
                else:  # Неизвестная структура, но есть payload
                    current_alice_text_output = f"(Не удалось извлечь текст, полный 'response': {alice_response_payload})"
            else:  # Если response_data['response'] не словарь или отсутствует
                current_alice_text_output = "(Нет ответа или неизвестная ошибка структуры)"

            print(f"[Алиса] < {current_alice_text_output}")

            # Сохраняем текст для TTS, если он не является сообщением об ошибке или техническим сообщением
            if isinstance(current_alice_text_output, str) and \
                    not current_alice_text_output.startswith("(") and \
                    alice_response_payload and not alice_response_payload.get('error'):
                last_alice_response_text_for_tts = current_alice_text_output
            else:
                last_alice_response_text_for_tts = None


    except ConnectionError as e:
        print(f"Ошибка подключения: {e}")
    except KeyboardInterrupt:  # Обработка Ctrl+C на более высоком уровне (например, во время подключения)
        print("\nВыход по команде пользователя (Ctrl+C)...")
    except Exception as e:
        import traceback
        print(f"Произошла непредвиденная ошибка: {e}")
        print("Детали ошибки:")
        traceback.print_exc()
    finally:
        print("----------------------------------------------------")
        if is_client_connected:
            print("Закрытие соединения с Яндекс Алисой...")
            client.close()
        else:
            # Это сообщение может появиться, если ошибка произошла до успешного client.connect()
            print("Соединение не было установлено или уже было закрыто ранее.")
        print("Программа завершена.")


if __name__ == '__main__':
    main_terminal_chat()
