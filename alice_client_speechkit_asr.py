import asyncio
import websockets
import json
import uuid
import time
import struct
import pyaudio  # Для работы с микрофоном
import functools

# ==============================================================================
# Конфигурация (ЗАМЕНИТЕ СВОИМИ ЗНАЧЕНИЯМИ)
# ==============================================================================
DEFAULT_SERVER_URL = 'wss://uniproxy.alice.ya.ru/uni.ws'  # или тестовый стенд
# OAuth-токен Яндекса для аутентификации в System.SynchronizeState
USER_OAUTH_TOKEN = "y0__xCAQAAAAAxxxxxxxxxxxx_xxxxxxxxX"  # <<< ЗАМЕНИТЕ НА ВАШ OAuth ТОКЕН
# Ключ для сервиса ASR (может быть другим, в примерах "developers-simple-key")
ASR_SERVICE_KEY = "developers-simple-key"

# Параметры аудио с микрофона
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 16000
CHUNK_SIZE = 1024 * 2  # 2048 байт = 1024 сэмпла * 2 байта/сэмпл (16 бит) = 64 мс при 16кГц
# ==============================================================================

# Флаг для управления активностью аудиопотока и основного цикла
asr_active = True


class YandexASRClientError(Exception):
    pass


class YandexASRClient:
    def __init__(self, server_url, oauth_token, asr_key, loop):
        self.server_url = server_url
        self.oauth_token = oauth_token
        self.asr_key = asr_key
        self._ws = None
        self._is_connected = False
        self._receive_loop_task = None
        self.device_uuid = "f" * 16 + str(uuid.uuid4()).replace("-", "")[16:32]
        self.loop = loop  # asyncio event loop

        # Callbacks
        self.partial_result_callback = lambda text, msg_id: print(f"  Partial ({msg_id[-6:]}): {text}")
        self.final_result_callback = lambda text, msg_id: print(
            f"  Final ({msg_id[-6:]}): {text}\n--- Listening for next utterance ---")

        # ASR session state
        self._current_asr_message_id = None
        self._current_stream_id_counter = 0  # Счетчик для генерации streamId
        self._utterance_in_progress = False  # True, когда ожидаем аудио для текущей ASR сессии

    async def connect(self):
        if self._is_connected and self._ws:
            print("Уже подключен.")
            return
        try:
            print(f"Подключение к {self.server_url}...")
            self._ws = await websockets.connect(
                self.server_url,
                ping_interval=20,
                ping_timeout=10
            )
            self._is_connected = True
            self._receive_loop_task = self.loop.create_task(self._receive_loop())
            print("Подключено!")
            await self._send_system_synchronize_state()
        except Exception as e:
            self._is_connected = False
            global asr_active
            asr_active = False
            raise YandexASRClientError(f"Не удалось подключиться к {self.server_url}: {e}")

    async def _send_system_synchronize_state(self):
        message_id = str(uuid.uuid4())
        event = {
            "event": {
                "header": {
                    "namespace": "System",
                    "name": "SynchronizeState",
                    "messageId": message_id,
                },
                "payload": {
                    "auth_token": self.oauth_token,
                    "uuid": self.device_uuid,
                }
            }
        }
        await self._send_json_event(event)
        print("System.SynchronizeState отправлен.")

    async def _send_json_event(self, event_data_dict):
        if not self._ws or not self._is_connected:
            raise YandexASRClientError("WebSocket не подключен.")
        try:
            # print(f"Отправка JSON: {json.dumps(event_data_dict)}")
            await self._ws.send(json.dumps(event_data_dict))
        except Exception as e:
            self._is_connected = False  # Важно при ошибке отправки
            global asr_active
            asr_active = False
            raise YandexASRClientError(f"Ошибка при отправке JSON события: {e}")

    async def _receive_loop(self):
        global asr_active
        if not self._ws:
            return
        try:
            async for raw_message in self._ws:
                if not asr_active: break  # Проверка флага для выхода

                if isinstance(raw_message, str):
                    try:
                        message_json = json.loads(raw_message)
                        if "directive" in message_json:
                            await self._on_directive(message_json["directive"])
                    except json.JSONDecodeError:
                        print(f"Не удалось декодировать JSON: {raw_message[:200]}...")
                    except Exception as e:
                        print(f"Ошибка обработки JSON сообщения: {e}")
                # ... (другие типы сообщений, если нужно)
        except websockets.exceptions.ConnectionClosed as e:
            print(f"Соединение закрыто: {e}")
        except Exception as e:
            if asr_active:  # Логируем ошибку только если мы еще активны
                print(f"Ошибка в цикле приема сообщений: {e}")
        finally:
            self._is_connected = False
            asr_active = False  # Останавливаем все при проблемах с циклом приема

    async def _on_directive(self, directive):
        header = directive.get("header", {})
        name = header.get("name")
        namespace = header.get("namespace")
        ref_message_id = header.get("refMessageId")

        if namespace == "ASR" and name == "Result":
            if ref_message_id != self._current_asr_message_id and self._utterance_in_progress:
                # Это может быть запоздавший ответ от предыдущей сессии ASR
                # print(f"Info: Получен ASR.Result для старого/неожиданного messageId: {ref_message_id}, текущий: {self._current_asr_message_id}")
                return  # Игнорируем его, если он не для текущей активной сессии

            payload = directive.get("payload", {})
            response_code = payload.get("responseCode")
            end_of_utt = payload.get("endOfUtt", False)  # По умолчанию False, если отсутствует

            text = ""
            normalized_text = ""
            if response_code == "OK" and payload.get("recognition"):
                for rec_variant in payload["recognition"]:  # Обычно один вариант в partial/final
                    text = " ".join(word.get("value", "") for word in rec_variant.get("words", []))
                    normalized_text = rec_variant.get("normalized")
                    break  # Берем первый вариант

            if end_of_utt:
                if self.final_result_callback:
                    self.final_result_callback(normalized_text or text, ref_message_id)
                if self._utterance_in_progress:  # Только если мы действительно ждали этот результат
                    await self._send_stream_control(self._current_asr_message_id, self._current_stream_id_counter)
                    self._utterance_in_progress = False  # Сигнал для основного цикла начать новую сессию
            else:  # Частичный результат
                if response_code == "OK":
                    if self.partial_result_callback:
                        self.partial_result_callback(normalized_text or text, ref_message_id)
                # Если response_code не OK для частичного, можно логировать, но не прерывать сессию
                # elif response_code: # Не "OK"
                #     print(f"Предупреждение: ASR.Result (частичный) с кодом {response_code} для {ref_message_id}: {payload.get('message')}")

    async def _send_stream_control(self, asr_msg_id, stream_id):
        if not asr_msg_id: return
        print(f"Отправка Streamcontrol для msg_id={asr_msg_id}, stream_id={stream_id}")
        stream_control_event = {
            "streamcontrol": {
                "streamId": stream_id,
                "action": 0,
                "reason": 0,
                "messageId": asr_msg_id
            }
        }
        try:
            await self._send_json_event(stream_control_event)
        except YandexASRClientError as e:
            print(f"Ошибка при отправке streamcontrol: {e}")
            # Соединение, вероятно, уже помечено как неактивное

    async def run_realtime_asr(self, audio_queue):
        global asr_active
        if not self._is_connected:
            try:
                await self.connect()
            except YandexASRClientError as e:
                print(f"Не удалось инициализировать ASR сессию из-за ошибки подключения: {e}")
                asr_active = False  # Устанавливаем флаг для остановки микрофона
                return

        print("\n--- Нажмите Ctrl+C для остановки ---")

        while asr_active and self._is_connected:
            self._current_stream_id_counter += 1  # Используем новый stream ID для каждой новой сессии
            current_stream_id = self._current_stream_id_counter
            self._current_asr_message_id = str(uuid.uuid4())
            self._utterance_in_progress = True

            recognize_event = {
                "event": {
                    "header": {
                        "namespace": "ASR", "name": "Recognize",
                        "messageId": self._current_asr_message_id,
                        "streamId": current_stream_id,
                    },
                    "payload": {
                        "lang": "ru-RU", "topic": "general",
                        "application": "python_realtime_mic_client",
                        "format": f"audio/x-pcm;bit=16;rate={RATE}",
                        "key": self.asr_key,
                        "advancedASROptions": {"partial_results": True, "capitalize": False}
                    }
                }
            }
            try:
                await self._send_json_event(recognize_event)
            except YandexASRClientError as e:
                print(f"Не удалось отправить ASR.Recognize: {e}")
                asr_active = False  # Остановка при ошибке
                break

            # print(f"Новая ASR сессия: msg_id={self._current_asr_message_id}, stream_id={current_stream_id}")

            # Цикл отправки аудио для текущей реплики
            while asr_active and self._utterance_in_progress and self._is_connected:
                try:
                    chunk = await asyncio.wait_for(audio_queue.get(), timeout=0.1)
                    if chunk is None:  # Сигнал от микрофона для полной остановки
                        print("Получен сигнал остановки от микрофона.")
                        asr_active = False
                        if self._utterance_in_progress:  # Если реплика еще была "в процессе"
                            await self._send_stream_control(self._current_asr_message_id, current_stream_id)
                        self._utterance_in_progress = False
                        break  # Выход из цикла отправки аудио

                    packed_stream_id = struct.pack(">I", current_stream_id)
                    await self._ws.send(packed_stream_id + chunk)
                    audio_queue.task_done()
                except asyncio.TimeoutError:
                    continue  # Просто проверяем флаги и продолжаем ждать аудио
                except websockets.exceptions.ConnectionClosed:
                    print("Соединение закрыто во время отправки аудио.")
                    asr_active = False  # Остановка
                    self._utterance_in_progress = False
                    break
                except Exception as e:
                    print(f"Ошибка при отправке аудио чанка: {e}")
                    asr_active = False  # Остановка при других ошибках
                    self._utterance_in_progress = False
                    break

            if not self._utterance_in_progress and self._current_asr_message_id and not asr_active:
                # Если utterance_in_progress был сброшен (например, ошибкой), но asr_active еще не обработан
                # или если мы останавливаемся и реплика была в процессе
                pass  # Stream control уже должен был быть отправлен или будет отправлен при выходе

        print("Цикл ASR завершен.")
        if self._is_connected and self._current_asr_message_id and self._utterance_in_progress:
            # Если мы вышли из цикла asr_active, но реплика не была формально завершена сервером
            print("Завершение последней активной сессии ASR...")
            await self._send_stream_control(self._current_asr_message_id, self._current_stream_id_counter)

    async def close(self):
        global asr_active
        asr_active = False  # Убедимся, что все циклы знают об остановке

        print("Закрытие клиента...")
        if self._receive_loop_task and not self._receive_loop_task.done():
            self._receive_loop_task.cancel()
            try:
                await self._receive_loop_task
            except asyncio.CancelledError:
                pass  # Ожидаемо
        if self._ws and self._is_connected:
            try:
                # Попытка отправить streamcontrol для последней активной сессии, если она была
                if self._current_asr_message_id and self._utterance_in_progress:
                    await self._send_stream_control(self._current_asr_message_id, self._current_stream_id_counter)
                await self._ws.close()
                print("Соединение WebSocket успешно закрыто.")
            except websockets.exceptions.ConnectionClosedOK:
                pass  # Уже закрыто
            except Exception as e:
                print(f"Ошибка при закрытии WebSocket: {e}")
        self._is_connected = False
        self._ws = None
        print("Клиент полностью остановлен.")


async def microphone_worker(audio_queue, loop, stop_event):
    """Задача, читающая аудио с микрофона и кладущая его в очередь."""
    global asr_active
    p = pyaudio.PyAudio()
    stream = None
    try:
        stream = await loop.run_in_executor(None, functools.partial(p.open,
                                                                    format=FORMAT,
                                                                    channels=CHANNELS,
                                                                    rate=RATE,
                                                                    input=True,
                                                                    frames_per_buffer=CHUNK_SIZE
                                                                    ))
        print("Микрофон активирован. Говорите...")

        while asr_active and not stop_event.is_set():
            try:
                # Чтение данных в отдельном потоке, чтобы не блокировать event loop
                data = await loop.run_in_executor(None, stream.read, CHUNK_SIZE, False)
                await audio_queue.put(data)
            except IOError as e:
                # Эта ошибка может возникать, если буфер pyaudio переполняется.
                # Уменьшение CHUNK_SIZE или увеличение частоты опроса может помочь,
                # но здесь мы просто логируем и продолжаем, если возможно.
                print(f"Ошибка чтения с микрофона (IOError): {e}. Поток может быть остановлен.")
                # asr_active = False # Раскомментируйте, если хотите останавливаться при ошибках микрофона
                break
            except Exception as e:
                print(f"Неожиданная ошибка чтения с микрофона: {e}")
                # asr_active = False
                break

    except Exception as e:
        print(f"Критическая ошибка инициализации/работы микрофона: {e}")
        asr_active = False  # Остановка основного цикла ASR
    finally:
        print("Остановка микрофона...")
        if stream:
            await loop.run_in_executor(None, stream.stop_stream)
            await loop.run_in_executor(None, stream.close)
        await loop.run_in_executor(None, p.terminate)
        await audio_queue.put(None)  # Сигнал для ASR клиента о завершении аудиопотока


async def main_async():
    global asr_active
    asr_active = True  # Устанавливаем в начале

    if not USER_OAUTH_TOKEN or "xxxxxxxxxx" in USER_OAUTH_TOKEN:
        print("Пожалуйста, укажите ваш OAuth-токен в переменной USER_OAUTH_TOKEN.")
        return
    if not ASR_SERVICE_KEY or ASR_SERVICE_KEY == "developers-simple-key":
        print("ПРЕДУПРЕЖДЕНИЕ: используется стандартный ключ ASR 'developers-simple-key'.")

    loop = asyncio.get_running_loop()
    audio_queue = asyncio.Queue(maxsize=100)  # Ограничиваем размер очереди
    stop_event = asyncio.Event()  # Для graceful shutdown микрофона

    client = YandexASRClient(DEFAULT_SERVER_URL, USER_OAUTH_TOKEN, ASR_SERVICE_KEY, loop)

    mic_task = None
    asr_task = None

    try:
        # Запускаем микрофон и ASR клиент параллельно
        mic_task = loop.create_task(microphone_worker(audio_queue, loop, stop_event))
        asr_task = loop.create_task(client.run_realtime_asr(audio_queue))

        # Ожидаем завершения одной из задач (или KeyboardInterrupt)
        # Если asr_active станет False, задачи должны завершиться сами
        done, pending = await asyncio.wait(
            [mic_task, asr_task],
            return_when=asyncio.FIRST_COMPLETED
        )

        for task in pending:
            task.cancel()  # Отменяем оставшиеся задачи

        # Собираем результаты/исключения, чтобы избежать unhandled exceptions
        for task in done:
            if task.exception():
                # print(f"Задача завершилась с исключением: {task.exception()}")
                pass  # Ошибки уже должны быть залогированы внутри задач

    except KeyboardInterrupt:
        print("\nПрограмма прервана пользователем (Ctrl+C)...")
    except websockets.exceptions.InvalidStatusCode as e:
        print(f"Ошибка подключения WebSocket (InvalidStatusCode): {e.status_code} {e.headers}")
        if e.status_code == 401:
            print("  Это может означать проблемы с аутентификацией (неверный OAuth-токен?).")
    except ConnectionRefusedError:
        print(f"Ошибка подключения: сервер {client.server_url if client else DEFAULT_SERVER_URL} отказал в соединении.")
    except Exception as e:
        import traceback
        print(f"Произошла непредвиденная ошибка в main_async: {e}")
        traceback.print_exc()
    finally:
        print("Начало процедуры завершения...")
        asr_active = False  # Главный флаг остановки
        stop_event.set()  # Сигнал для микрофона остановиться

        if client and client._is_connected:
            print("Ожидание закрытия клиента ASR...")
            await client.close()  # Закрываем соединение с сервером
        elif client:  # Если клиент был создан, но не подключен, или соединение уже разорвано
            await client.close()  # Попытка очистки

        # Даем задачам немного времени на завершение после установки флагов
        if mic_task and not mic_task.done():
            print("Ожидание завершения задачи микрофона...")
            try:
                await asyncio.wait_for(mic_task, timeout=2.0)
            except asyncio.TimeoutError:
                print("Таймаут ожидания задачи микрофона.")
            except asyncio.CancelledError:
                pass
        if asr_task and not asr_task.done():
            print("Ожидание завершения задачи ASR...")
            try:
                await asyncio.wait_for(asr_task, timeout=2.0)
            except asyncio.TimeoutError:
                print("Таймаут ожидания задачи ASR.")
            except asyncio.CancelledError:
                pass
        print("Процедура завершения окончена.")


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        # Этот блок может не всегда срабатывать, если KeyboardInterrupt уже перехвачен в main_async
        print("\nПрограмма остановлена (внешний KeyboardInterrupt).")
    finally:
        print("Программа полностью завершена.")