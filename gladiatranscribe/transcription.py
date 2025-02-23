import asyncio
from io import BytesIO
from typing import List, Type

import aiohttp
import mautrix.crypto.attachments
from maubot import Plugin, MessageEvent
from maubot.handlers import command, event
from mautrix.client import Client as MatrixClient
from mautrix.types import MessageType, EventType, MediaMessageEventContent, EncryptedFile
from mautrix.util.config import BaseProxyConfig

from .config import Config

UPLOAD_URL = "https://api.gladia.io/v2/upload"
TRANSCRIPTION_URL = "https://api.gladia.io/v2/pre-recorded"

async def download_encrypted_media(file: EncryptedFile, client: MatrixClient) -> bytes:
    """
    Download an encrypted media file
    :param file: The `EncryptedFile` instance, from MediaMessageEventContent.file.
    :param client: The Matrix client. Can be accessed via MessageEvent.client
    :return: The media file as bytes.
    """
    return mautrix.crypto.attachments.decrypt_attachment(
        await client.download_media(file.url),
        file.key.key,
        file.hashes['sha256'],
        file.iv
    )


async def download_unencrypted_media(url, client: MatrixClient) -> bytes:
    """
    Download an unencrypted media file
    :param url: The media file mxc url, from MediaMessageEventContent.url.
    :param client: The Matrix client. Can be accessed via MessageEvent.client
    :return: The media file as bytes.
    """
    return await client.download_media(url)

class TranscriptionPoll():
    def __init__(self, url: str, evt: MessageEvent):
        self.url = url
        self.evt = evt


class GladiaTranscribe(Plugin):
    config: Config

    polls: List[TranscriptionPoll] = []
    _cancellation_token: asyncio.CancellationToken
    _poll_task: asyncio.Task   

    async def start(self) -> None:
        self.config.load_and_update()
        self._cancellation_token = asyncio.CancellationToken()
        self._poll_task = None

    @event.on(EventType.ROOM_MESSAGE)
    async def transcribe_audio_message(self, evt: MessageEvent) -> None:
        """
        Replies to any voice message with its transcription.
        """
        # Only reply to voice messages
        if evt.content.msgtype != MessageType.AUDIO:
            return

        content: MediaMessageEventContent = evt.content
        self.log.debug("A voice message was received.")

        if content.url:  # content.url exists. media is not encrypted
            data = await download_unencrypted_media(content.url, evt.client)
        elif content.file:  # content.file exists. media is encrypted
            data = await download_encrypted_media(content.file, evt.client)
        else:
            self.log.warning("A message with type audio was received, but no media was found.")
            return
        
        audio_url = await self.upload_audio(self, data)
        transcription_url = await self.request_transcription(audio_url)
        self.polls.append(TranscriptionPoll(transcription_url, evt))
        self.start_poll_task()


    async def upload_audio(self, data: bytes) -> str:
        """
        Uploads the audio file to the transcription API and sends the transcription as a reply.
        """
        # Header for API KEY
        self.http.headers["x-gladia-key"] = self.config['api_key']
        self.http.headers["Content-Type"] = "multipart/form-data"

        # initialize data for the POST request
        request_data = aiohttp.FormData()

        # Convert data bytes into a BytesIO object
        request_data.add_field('audio', BytesIO(data))
        response = await self.http.post(UPLOAD_URL, data=request_data)
        # Properly log if the response is not what expected
        if response.status != 200:
            self.log.error(F"Failed to upload audio: {response}")
            return
        response_json = await response.json()
        audio_url = response_json['audio_url']
        self.log.debug(F"Audio uploaded to {audio_url}")
        return audio_url

    async def request_transcription(self, audio_url: str) -> str:
        """
        Requests the transcription of the audio file from the transcription API.
        """
        self.http.headers["x-gladia-key"] = self.config['api_key']
        # initialize data for the POST request
        request_data = aiohttp.FormData()
        request_data.add_field('audio_url', audio_url)
        request_data.add_field('enable_code_switching', True)
        request_data.add_field('code_switching_config', {"languages": self.config['languages']})

        response = await self.http.post(TRANSCRIPTION_URL, data=request_data)
        if response.status != 200:
            self.log.error(F"Failed to request transcription: {response}")
            return
        response_json = await response.json()
        transcription_url = response_json['result_url']
        self.log.debug(F"Transcription requested: {transcription_url}")
        return transcription_url

    async def _poll_transcriptions(self):
        """
        Polls the transcription API for the transcriptions of the uploaded audio files.
        """
        while not self._cancellation_token.is_cancelled():
            try:
                newPolls = []
                for poll in self.polls:
                    self.http.headers["x-gladia-key"] = self.config['api_key']
                    response = await self.http.get(poll.url)

                    response_json = await response.json()
                    if response_json['status'] == 'done':
                        await self.use_transcription(poll, response_json['result']['transcription'])
                    else:
                        newPolls.append(poll)
                        continue
                self.polls = newPolls
                if len(self.polls) == 0:
                    self._cancellation_token.cancel()
                    break
                await asyncio.sleep(self.config["poll_interval"], lookup=self._cancellation_token) # Sleep and check cancellation during sleep
            except asyncio.CancellationError:
                # Ignore CancellationError during sleep - this is expected when task is stopped
                pass
            except Exception as e:
                print(f"Error in periodic task: {e}")
                # Optionally handle errors here, maybe log them or implement retry logic
        print("Periodic task stopped.") # Indicate task has stopped gracefully

    async def use_transcription(self, poll: TranscriptionPoll, transc: str):
        """
        Sends the transcription as a reply to the message that was transcribed.
        """
        self.log.debug(F"Message transcribed: {transc}")

        # send transcription as reply
        await poll.evt.reply(transc)
        self.log.debug("Reply sent")

        
    def start_poll_task(self):
        """Starts the periodic task in a separate task."""
        if (
            (self._cancellation_token is not None and not self._cancellation_token.cancelled())
              or (self._poll_task is not None and not self._poll_task.done())
              ):
            print("Periodic task already running.")
            return
        self._cancellation_token = asyncio.CancellationToken()
        self._poll_task = self.loop.create_task(self._poll_transcriptions())
        print("Periodic task started.")

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config