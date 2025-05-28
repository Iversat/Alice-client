import websocket
import uuid
import json
import threading
import time
import datetime
from typing import Dict, Any, Set, List, Optional, Union

# ==============================================================================
DEFAULT_SERVER_URL = 'wss://uniproxy.alice.ya.ru/uni.ws'
REQUEST_TIMEOUT_SECONDS = 10
# ==============================================================================
PRESET_OAUTH_TOKEN: str = "y0__xCAQAAAAAxxxxxxxxxxxx_xxxxxxxxX"
# ==============================================================================


# ==============================================================================
# Класс UniproxyPython
# ==============================================================================
class UniproxyPython:
    """
    Низкоуровневый клиент для WebSocket-сервиса uniproxy.
    Управляет соединением, отправкой/получением сообщений JSON и бинарных данных (аудио).
    """

    def __init__(self):
        self.ws: Optional[websocket.WebSocketApp] = None
        self.requests: Dict[str, Dict[str, Any]] = {}  # message_id -> info
        self.streams: Dict[int, Dict[str, Any]] = {}  # stream_id -> info
        self.ws_thread: Optional[threading.Thread] = None
        self.is_connected_event = threading.Event()

    def connect(self, server_url: str, connection_timeout: float = 10.0) -> None:
        self.is_connected_event.clear()
        # Убираем ping_interval и ping_timeout из конструктора WebSocketApp
        self.ws = websocket.WebSocketApp(server_url, on_open=self._on_open, on_message=self._on_message,
                                         on_error=self._on_error, on_close=self._on_close)

        # Параметры для ping/pong передаются в run_forever
        # ping_interval должен быть больше ping_timeout
        self.ws_thread = threading.Thread(
            target=lambda: self.ws.run_forever(
                ping_interval=25,  # Отправлять ping каждые 20 секунд
                ping_timeout=15  # Ожидать pong-ответ в течение 10 секунд
            ),
            name="UniproxyWSThread"
        )
        self.ws_thread.daemon = True
        self.ws_thread.start()

        if not self.is_connected_event.wait(timeout=connection_timeout):
            if self.ws:
                try:
                    self.ws.close()
                except Exception:
                    pass
            raise ConnectionError(f"Не удалось подключиться к {server_url} за {connection_timeout} секунд.")

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        self.is_connected_event.set()

    def _on_error(self, ws: websocket.WebSocketApp, error: Exception) -> None:
        print(f"Ошибка WebSocket: {error}")
        for req_info in list(self.requests.values()):
            if req_info and not req_info['event'].is_set():
                req_info['data']['error'] = str(error)
                req_info['event'].set()
        self.is_connected_event.clear()

    def _on_close(self, ws: websocket.WebSocketApp, close_status_code: Optional[int], close_msg: Optional[str]) -> None:
        close_reason = f"Код: {close_status_code}, Сообщение: {close_msg}" if close_status_code else "Соединение закрыто"
        # print(f"WebSocket соединение закрыто. {close_reason}") # Для отладки
        for req_info in list(self.requests.values()):
            if req_info and not req_info['event'].is_set():
                req_info['data']['error'] = f"Соединение закрыто ({close_reason})"
                req_info['event'].set()
        self.is_connected_event.clear()

    def _on_buffer(self, data_buffer: bytes) -> None:
        if len(data_buffer) < 4: return
        stream_id = int.from_bytes(data_buffer[:4], byteorder='big')
        stream = self.streams.get(stream_id)
        if not stream: return
        stream['buffers'].append(data_buffer[4:])

    def _on_message(self, ws: websocket.WebSocketApp, message: Union[str, bytes]) -> None:
        if isinstance(message, bytes):
            self._on_buffer(message)
            return
        try:
            response_data = json.loads(message)
        except json.JSONDecodeError:
            # print(f"Ошибка декодирования JSON: {message[:200]}...") # Для отладки
            return

        request_info: Optional[Dict[str, Any]] = None
        if 'directive' in response_data:
            request_info = self._on_directive(response_data['directive'])
        elif 'streamcontrol' in response_data:
            request_info = self._on_streamcontrol(response_data['streamcontrol'])
        # else: # Для отладки неизвестных сообщений
        # print(f"Получено неизвестное JSON сообщение: {response_data}")

        if request_info and not request_info['needs'] and not request_info['event'].is_set():
            request_info['event'].set()

    def _on_directive(self, directive: Dict[str, Any]) -> Optional[Dict[str, Any]]:
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
                'message_id': ref_message_id, 'buffers': [], 'stream_id': header['streamId']
            }
        return request_info

    def _on_streamcontrol(self, streamcontrol: Dict[str, Any]) -> Optional[Dict[str, Any]]:
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
        if not self.is_connected_event.is_set() or not self.ws:
            raise ConnectionError("WebSocket не подключен.")
        message_id = str(uuid.uuid4())
        event_header = {"namespace": namespace, "name": name, "messageId": message_id}
        if header_extra: event_header.update(header_extra)
        event_message = {"event": {"header": event_header, "payload": payload}}
        # print(f"Отправка события: {json.dumps(event_message, ensure_ascii=False)}") # Для отладки
        self.ws.send(json.dumps(event_message))
        return message_id

    def receive_data(self, message_id: str, needs_list: List[str], timeout: float = 10.0) -> Dict[str, Any]:
        event = threading.Event()
        self.requests[message_id] = {
            'id': message_id, 'event': event, 'data': {'directives': [], 'audio': None},
            'needs': set(needs_list), 'timestamp': time.time()
        }
        if event.wait(timeout=timeout):
            request_data_snapshot = self.requests.pop(message_id)['data']
            if 'error' in request_data_snapshot:
                raise RuntimeError(f"Ошибка запроса {message_id}: {request_data_snapshot['error']}")
            return request_data_snapshot
        else:
            self.requests.pop(message_id, None)
            raise TimeoutError(f"Таймаут ответа для messageId {message_id} (ожидались: {needs_list})")

    def close(self) -> None:
        if self.ws:
            try:
                # print("Закрытие WebSocket соединения UniproxyPython...") # Для отладки
                self.ws.close()
            except Exception as e:
                # print(f"Ошибка при закрытии WebSocket UniproxyPython: {e}") # Для отладки
                pass  # Игнорируем ошибки при закрытии, если соединение уже разорвано
        if self.ws_thread and self.ws_thread.is_alive():
            self.ws_thread.join(timeout=1.0)  # Даем потоку время на завершение


# ==============================================================================
# Класс YandexAliceClientPython
# ==============================================================================
class YandexAliceClientPython:
    """
    Клиент для взаимодействия с Яндекс Алисой через Uniproxy.
    Предоставляет высокоуровневые методы для отправки команд и получения ответов.
    """

    def __init__(self, server_url: Optional[str] = None, oauth_token: Optional[str] = None):
        self.uniproxy = UniproxyPython()
        self.server_url: str = server_url or DEFAULT_SERVER_URL
        self.client_app_uuid: str = str(uuid.uuid4())
        self.device_uuid: str = str(uuid.uuid4())
        self.oauth_token: Optional[str] = oauth_token if oauth_token else None

    def set_oauth_token(self, token: str) -> None:
        self.oauth_token = token if token else None

    def connect(self) -> None:
        self.uniproxy.connect(self.server_url)

    def synchronize_state(self, lang: str = 'ru-RU', voice: str = 'levitan') -> str:
        """Синхронизирует состояние клиента с сервером Алисы."""
        payload = {
            "uuid": self.device_uuid,
            "lang": lang,
            "voice": voice,
            "clientTime": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "timezone": "Europe/Moscow"
        }
        if self.oauth_token:
            payload["auth_token"] = self.oauth_token
        return self.uniproxy.send_event('System', 'SynchronizeState', payload)

    def send_text(self, text: str, is_tts: bool = False) -> Dict[str, Any]:
        if not text: raise ValueError("Текст запроса не может быть пустым.")
        request_payload = {
            "request": {
                "voice_session": is_tts,
                "event": {
                    "type": "text_input",
                    "text": text
                }
            },
            "application": self._get_application_details()
        }
        if self.oauth_token:
            if 'request' not in request_payload: request_payload['request'] = {}
            if 'additional_options' not in request_payload['request']:
                request_payload['request']['additional_options'] = {}
            request_payload['request']['additional_options']['oauth_token'] = self.oauth_token

        message_id = self.uniproxy.send_event('Vins', 'TextInput', request_payload)
        needs = ['VinsResponse']
        if is_tts: needs.append('audio')  # This remains, as it's for Alice's spoken response

        try:
            response_data = self.uniproxy.receive_data(message_id, needs)
        except TimeoutError:
            return {"response": {"error": "Таймаут получения ответа от Алисы"}, "audio": None}
        except RuntimeError as e:
            return {"response": {"error": str(e)}, "audio": None}

        vins_response = None
        if response_data.get('directives'):
            for directive in response_data['directives']:
                if directive.get('header', {}).get('name') == 'VinsResponse':
                    vins_response = directive.get('payload', {}).get('response')
                    break

        if vins_response is None and 'error' not in response_data:
            vins_response = {"error": response_data.get('error', "Директива VinsResponse не найдена в ответе.")}
        elif vins_response is None and 'error' in response_data:
            vins_response = {"error": response_data['error']}

        return {"response": vins_response, "audio": response_data.get('audio')}

    def _get_application_details(self) -> Dict[str, str]:
        return {
            "app_id": "aliced_python_terminal", "app_version": "1.0.3", "os_version": "CLI",
            "platform": "python_terminal", "uuid": self.client_app_uuid, "lang": "ru-RU",
            "client_time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "timezone": "Europe/Moscow", "timestamp": str(int(time.time()))
        }

    def close(self) -> None:
        self.uniproxy.close()


# ==============================================================================
# Основная логика терминального чата
# ==============================================================================
def main_terminal_chat():
    token_from_config = PRESET_OAUTH_TOKEN.strip()
    active_oauth_token: Optional[str] = token_from_config if token_from_config else None

    if not active_oauth_token:
        print("Предупреждение: OAuth токен не указан. "
              "Некоторые функции могут работать некорректно.")
    else:
        print(f"Используется OAuth токен (первые 10 символов): {active_oauth_token[:10]}...")

    client = YandexAliceClientPython(oauth_token=active_oauth_token)
    is_client_connected = False

    try:
        print(f"Подключение к Яндекс Алисе ({client.server_url})...")
        client.connect()
        is_client_connected = True
        print("Синхронизация состояния...")
        sync_message_id = client.synchronize_state()
        # Можно добавить ожидание ответа на SynchronizeState, если это необходимо
        # Например, client.uniproxy.receive_data(sync_message_id, [], timeout=5)
        # Но для простого терминала это может быть избыточно.
        print(f"Состояние синхронизировано (ID сообщения: {sync_message_id}).")
        print("Подключено! Введите 'exit' или 'quit' для выхода.")
        print("----------------------------------------------------")

        while True:
            try:
                user_input = input("[Вы] > ").strip()
            except EOFError:
                print("\nВыход (EOF)...");
                break
            except KeyboardInterrupt:
                print("\nВыход (Ctrl+C)...");
                break

            if user_input.lower() in ['exit', 'quit']: break
            if not user_input: continue

            response_data = client.send_text(user_input,is_tts=False)
            alice_response_dict = response_data.get('response')
            current_text_output = None

            if isinstance(alice_response_dict, dict):
                if 'error' in alice_response_dict:
                    current_text_output = f"(Ошибка от Алисы: {alice_response_dict['error']})"
                else:
                    card_data = alice_response_dict.get('card')
                    text_from_card = None
                    if isinstance(card_data, dict):
                        text_from_card = card_data.get('text')

                    if text_from_card:
                        current_text_output = text_from_card
                    elif 'text' in alice_response_dict:
                        current_text_output = alice_response_dict['text']
                    else:
                        current_text_output = f"(Неизвестный формат ответа от Алисы: {alice_response_dict})"
            else:
                current_text_output = "(Нет ответа или неизвестная структура ответа)"

            print(f"[Алиса] < {current_text_output}")

    except ConnectionError as e:
        print(f"Ошибка подключения: {e}")
    except ValueError as e:
        print(f"Ошибка значения: {e}")
    except KeyboardInterrupt:
        print("\nВыход (Ctrl+C)...")
    except Exception as e:
        import traceback
        print(f"Непредвиденная ошибка: {e}")
        traceback.print_exc()
    finally:
        print("----------------------------------------------------")
        if is_client_connected:
            print("Закрытие соединения с Алисой...")
            client.close()
        else:
            print("Соединение с Алисой не было установлено или уже закрыто.")
        print("Программа терминального чата с Алисой завершена.")


if __name__ == '__main__':
    main_terminal_chat()