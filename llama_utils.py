from ollama import Client, AsyncClient, ChatResponse
import base64


def setup_client(host):
    return Client(host=f'http://{host}:11434', headers={'Content-Type': 'application/json'})


def list_models(client):
    model_data = client.list()
    models = []
    for m in model_data.models:
        models.append(m.model)
    return models


def ask_model(client, model, prompt):
    response = client.chat(model=model, messages=[
      {
        'role': 'user',
        'content': prompt,
      },
    ])
    return response


def ask_model_with_image(client, model, prompt, img_data):
    messages = [
        {
            "role": "user",
            "content": prompt,
            "images": [base64.b64encode(img_data).decode('utf-8')]
        }
    ]
    return client.chat(model=model,messages=messages)


async def async_ask_model(host, model, prompt):
    client = AsyncClient(host=f'http://{host}:11434', headers={'Content-Type': 'application/json'})
    try:
        response: ChatResponse = await client.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
        )
        # print(response.message["content"])
        return response
    except Exception as e:
        print(f"An error occurred: {e}")
        pass
    


def delete_model(client, model):
    client.delete(model)
    print(f'[+] Deleting {model}')


def download_model(client, model):
    print(f'[+] Pulling model: {model}')
    client.pull(model)


from dotenv import load_dotenv
import websocket
import urllib.request
import urllib

import uuid
import json
import os

load_dotenv()
URL = os.getenv('URL')
comfy = os.getenv('COMFY')


def get_history(prompt_id):
    with urllib.request.urlopen(f"http://{comfy}:8188/history/{prompt_id}") as response:
        return json.loads(response.read())


def queue_prompt(prompt):
    # generic image creation
    p = {"prompt": prompt, "client_id": client_id}
    data = json.dumps(p).encode('utf-8')
    req = urllib.request.Request(f"http://{comfy}:8188/prompt", data=data)
    return json.loads(urllib.request.urlopen(req).read())


def get_image(filename, subfolder, folder_type):
    data = {"filename": filename, "subfolder": subfolder, "type": folder_type}
    url_values = urllib.parse.urlencode(data)
    with urllib.request.urlopen(f"http://{comfy}:8188/view?{url_values}") as response:
        return response.read()


def find_images(prompt_id):
    # Since a ComfyUI workflow may contain multiple SaveImage nodes,
    # and each SaveImage node might save multiple images,
    # we need to iterate through all outputs to collect all generated images
    output_images = {}
    history = get_history(prompt_id)
    node_output = history[prompt_id]['outputs']
    images_output = {}
    for key in node_output.keys():
        fields = node_output[key]
        if 'images' in fields:
            for image in node_output[key]['images']:
                image_data = get_image(image['filename'], image['subfolder'], image['type'])
                images_output[image['filename']] = image_data
            output_images[prompt_id] = images_output
    return images_output

try:
    client_id = str(uuid.uuid4())  # Generate a unique client ID
    ws = websocket.WebSocket()
    ws.connect(f"ws://{comfy}:8188/ws?clientId={client_id}")
except:
    pass







