import json
import os
from threading import Thread

import extensions.openai.completions as OAIcompletions
import extensions.openai.edits as OAIedits
import extensions.openai.embeddings as OAIembeddings
import extensions.openai.images as OAIimages
import extensions.openai.models as OAImodels
import extensions.openai.moderations as OAImoderations
import speech_recognition as sr
import uvicorn
from extensions import openai
from extensions.openai.defaults import clamp, default, get_default_req_params
from extensions.openai.errors import ServiceUnavailableError
from extensions.openai.tokens import token_count, token_decode, token_encode
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from modules import shared
from pydub import AudioSegment
from sse_starlette import EventSourceResponse

from .typing import (
    ChatCompletionChunkChoiceObject,
    ChatCompletionChunkResponse,
    ChatCompletionRequest,
    ChatCompletionResponse,
    CompletionRequest,
    CompletionResponse,
    to_dict,
    to_json
)

params = {
    # default params
    'port': 5000,
    'embedding_device': 'cpu',
    'embedding_model': 'all-mpnet-base-v2',

    # optional params
    'sd_webui_url': '',
    'debug': 0
}

app = FastAPI()

# Configure CORS settings to allow all origins, methods, and headers
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "HEAD", "OPTIONS", "POST", "PUT"],
    allow_headers=[
        "Origin",
        "Accept",
        "X-Requested-With",
        "Content-Type",
        "Access-Control-Request-Method",
        "Access-Control-Request-Headers",
        "Authorization",
    ],
)


@app.options("/")
async def options_route():
    return JSONResponse(content="OK")


@app.post('/v1/completions', response_model=CompletionResponse)
@app.post('/v1/generate', response_model=CompletionResponse)
async def openai_completions(request: Request, request_data: CompletionRequest):
    body = await request.json()
    path = request.url.path
    is_legacy = "/generate" in path

    if request_data.stream:
        async def generator():
            response = OAIcompletions.stream_completions(to_dict(request_data), is_legacy=is_legacy)
            for resp in response:
                yield {"data": json.dumps(resp)}

        return EventSourceResponse(generator())  # sse

    else:
        response = OAIcompletions.completions(to_dict(request_data), is_legacy=is_legacy)
        return JSONResponse(response)


@app.post('/v1/chat/completions', response_model=ChatCompletionResponse)
async def openai_chat_completions(request: Request, request_data: ChatCompletionRequest):
    body = await request.json()
    path = request.url.path
    is_legacy = "/generate" in path

    if request_data.stream:
        async def generator():
            response = OAIcompletions.stream_chat_completions(to_dict(request_data), is_legacy=is_legacy)
            for resp in response:
                yield {"data": json.dumps(resp)}

        return EventSourceResponse(generator())  # sse

    else:
        response = OAIcompletions.chat_completions(to_dict(request_data), is_legacy=is_legacy)
        return JSONResponse(response)


@app.get("/v1/models")
@app.get("/v1/engines")
async def handle_models(request: Request):
    path = request.url.path
    is_legacy = 'engines' in path
    is_list = request.url.path.split('?')[0].split('#')[0] in ['/v1/engines', '/v1/models']
    if is_legacy and not is_list:
        model_name = path[path.find('/v1/engines/') + len('/v1/engines/'):]
        resp = OAImodels.load_model(model_name)
    elif is_list:
        resp = OAImodels.list_models(is_legacy)
    else:
        model_name = path[len('/v1/models/'):]
        resp = OAImodels.model_info(model_name)

    return JSONResponse(content=resp)


@app.get('/v1/billing/usage')
def handle_billing_usage():
    '''
    Ex. /v1/dashboard/billing/usage?start_date=2023-05-01&end_date=2023-05-31
    '''
    return JSONResponse(content={"total_usage": 0})


@app.post('/v1/audio/transcriptions')
async def handle_audio_transcription(request: Request):
    r = sr.Recognizer()

    form = await request.form()
    audio_file = await form["file"].read()
    audio_data = AudioSegment.from_file(audio_file)

    # Convert AudioSegment to raw data
    raw_data = audio_data.raw_data

    # Create AudioData object
    audio_data = sr.AudioData(raw_data, audio_data.frame_rate, audio_data.sample_width)
    whipser_language = form.getvalue('language', None)
    whipser_model = form.getvalue('model', 'tiny')  # Use the model from the form data if it exists, otherwise default to tiny

    transcription = {"text": ""}

    try:
        transcription["text"] = r.recognize_whisper(audio_data, language=whipser_language, model=whipser_model)
    except sr.UnknownValueError:
        print("Whisper could not understand audio")
        transcription["text"] = "Whisper could not understand audio UnknownValueError"
    except sr.RequestError as e:
        print("Could not request results from Whisper", e)
        transcription["text"] = "Whisper could not understand audio RequestError"

    return JSONResponse(content=transcription)


@app.post('/v1/edits')
async def handle_edits(request: Request):
    '''
    deprecated
    '''

    body = await request.json()
    instruction = body["instruction"]
    input = body.get("input", "")
    temperature = clamp(default(body, "temperature", get_default_req_params()["temperature"]), 0.001, 1.999)  # fixup absolute 0.0
    top_p = clamp(default(body, "top_p", get_default_req_params()["top_p"]), 0.001, 1.0)

    response = await OAIedits.edits(instruction, input, temperature, top_p)
    return JSONResponse(response)


@app.post('/v1/images/generations')
async def handle_image_generation(request: Request):

    if not os.environ.get('SD_WEBUI_URL', params.get('sd_webui_url', '')):
        raise ServiceUnavailableError("Stable Diffusion not available. SD_WEBUI_URL not set.")

    body = await request.json()
    prompt = body['prompt']
    size = default(body, 'size', '1024x1024')
    response_format = default(body, 'response_format', 'url')  # or b64_json
    n = default(body, 'n', 1)  # ignore the batch limits of max 10

    response = await OAIimages.generations(prompt=prompt, size=size, response_format=response_format, n=n)
    return JSONResponse(response)


@app.post("/v1/embeddings")
async def handle_embeddings(request: Request):
    body = await request.json()
    encoding_format = body.get("encoding_format", "")

    input = body.get('input', body.get('text', ''))
    if not input:
        raise HTTPException(status_code=400, detail="Missing required argument input")

    if type(input) is str:
        input = [input]

    response = OAIembeddings.embeddings(input, encoding_format)
    return JSONResponse(response)


@app.post("/v1/moderations")
async def handle_moderations(request: Request):
    body = await request.json()
    input = body["input"]
    if not input:
        raise HTTPException(status_code=400, detail="Missing required argument input")

    response = OAImoderations.moderations(input)
    return JSONResponse(response)


@app.post("/api/v1/token-count")
async def handle_token_count(request: Request):
    body = await request.json()
    response = token_count(body['prompt'])
    return JSONResponse(response)


@app.post("/api/v1/token/encode")
async def handle_token_encode(request: Request):
    body = await request.json()
    encoding_format = body.get("encoding_format", "")
    response = token_encode(body["input"], encoding_format)
    return JSONResponse(response)


@app.post("/api/v1/token/decode")
async def handle_token_decode(request: Request):
    body = await request.json()
    encoding_format = body.get("encoding_format", "")
    response = token_decode(body["input"], encoding_format)
    return JSONResponse(response, no_debug=True)


def run_server():
    port = int(os.environ.get('OPENEDAI_PORT', params.get('port', 5000)))
    server_addr = '0.0.0.0' if shared.args.listen else '127.0.0.1'

    ssl_certfile = os.environ.get('OPENEDAI_CERT_PATH', shared.args.ssl_certfile)
    ssl_keyfile = os.environ.get('OPENEDAI_KEY_PATH', shared.args.ssl_keyfile)
    ssl_verify = True if (ssl_keyfile and ssl_certfile) else False

    if shared.args.share:
        try:
            from flask_cloudflared import _run_cloudflared
            public_url = _run_cloudflared(port, port + 1)
            print(f'OpenAI compatible API ready at: OPENAI_API_BASE={public_url}/v1')
        except ImportError:
            print('You should install flask_cloudflared manually')
    else:
        if ssl_verify:
            print(f'OpenAI compatible API ready at: OPENAI_API_BASE=https://{server_addr}:{port}/v1')
        else:
            print(f'OpenAI compatible API ready at: OPENAI_API_BASE=http://{server_addr}:{port}/v1')

    uvicorn.run(app, host=server_addr, port=port, ssl_certfile=ssl_certfile, ssl_keyfile=ssl_keyfile)


def setup():
    Thread(target=run_server, daemon=True).start()
