# ruff: noqa: E402
# Above allows ruff to ignore E402: module level import not at top of file

import re
import tempfile

import click
import gradio as gr
import numpy as np
import soundfile as sf
import torchaudio
from cached_path import cached_path
from transformers import AutoModelForCausalLM, AutoTokenizer

try:
    import spaces

    USING_SPACES = True
except ImportError:
    USING_SPACES = False


def gpu_decorator(func):
    if USING_SPACES:
        return spaces.GPU(func)
    else:
        return func


from f5_tts.model import DiT, UNetT
from f5_tts.infer.utils_infer import (
    load_vocoder,
    load_model,
    preprocess_ref_audio_text,
    infer_process,
    remove_silence_for_generated_wav,
    save_spectrogram,
)

vocos = load_vocoder()


# load models
F5TTS_model_cfg = dict(dim=1024, depth=22, heads=16, ff_mult=2, text_dim=512, conv_layers=4)
F5TTS_ema_model = load_model(
    DiT, F5TTS_model_cfg, str(cached_path("hf://SWivid/F5-TTS/F5TTS_Base/model_1200000.safetensors"))
)

E2TTS_model_cfg = dict(dim=1024, depth=24, heads=16, ff_mult=4)
E2TTS_ema_model = load_model(
    UNetT, E2TTS_model_cfg, str(cached_path("hf://SWivid/E2-TTS/E2TTS_Base/model_1200000.safetensors"))
)

chat_model_state = None
chat_tokenizer_state = None


@gpu_decorator
def generate_response(messages, model, tokenizer):
    """Generate response using Qwen"""
    text = tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
    )

    model_inputs = tokenizer([text], return_tensors="pt").to(model.device)
    generated_ids = model.generate(
        **model_inputs,
        max_new_tokens=512,
        temperature=0.7,
        top_p=0.95,
    )

    generated_ids = [
        output_ids[len(input_ids) :] for input_ids, output_ids in zip(model_inputs.input_ids, generated_ids)
    ]
    return tokenizer.batch_decode(generated_ids, skip_special_tokens=True)[0]


@gpu_decorator
def infer(ref_audio_orig, ref_text, gen_text, model, remove_silence, cross_fade_duration=0.15, speed=1):
    ref_audio, ref_text = preprocess_ref_audio_text(ref_audio_orig, ref_text, show_info=gr.Info)

    if model == "F5-TTS":
        ema_model = F5TTS_ema_model
    elif model == "E2-TTS":
        ema_model = E2TTS_ema_model

    final_wave, final_sample_rate, combined_spectrogram = infer_process(
        ref_audio,
        ref_text,
        gen_text,
        ema_model,
        cross_fade_duration=cross_fade_duration,
        speed=speed,
        show_info=gr.Info,
        progress=gr.Progress(),
    )

    # Remove silence
    if remove_silence:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
            sf.write(f.name, final_wave, final_sample_rate)
            remove_silence_for_generated_wav(f.name)
            final_wave, _ = torchaudio.load(f.name)
        final_wave = final_wave.squeeze().cpu().numpy()

    # Save the spectrogram
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_spectrogram:
        spectrogram_path = tmp_spectrogram.name
        save_spectrogram(combined_spectrogram, spectrogram_path)

    return (final_sample_rate, final_wave), spectrogram_path


with gr.Blocks() as app_credits:
    gr.Markdown("""
# Credits

* [mrfakename](https://github.com/fakerybakery) for the original [online demo](https://huggingface.co/spaces/mrfakename/E2-F5-TTS)
* [RootingInLoad](https://github.com/RootingInLoad) for initial chunk generation and podcast app exploration
* [jpgallegoar](https://github.com/jpgallegoar) for multiple speech-type generation & voice chat
""")
with gr.Blocks() as app_tts:
    gr.Markdown("# Batched TTS")
    ref_audio_input = gr.Audio(label="Reference Audio", type="filepath")
    gen_text_input = gr.Textbox(label="Text to Generate", lines=10)
    model_choice = gr.Radio(choices=["F5-TTS", "E2-TTS"], label="Choose TTS Model", value="F5-TTS")
    generate_btn = gr.Button("Synthesize", variant="primary")
    with gr.Accordion("Advanced Settings", open=False):
        ref_text_input = gr.Textbox(
            label="Reference Text",
            info="Leave blank to automatically transcribe the reference audio. If you enter text it will override automatic transcription.",
            lines=2,
        )
        remove_silence = gr.Checkbox(
            label="Remove Silences",
            info="The model tends to produce silences, especially on longer audio. We can manually remove silences if needed. Note that this is an experimental feature and may produce strange results. This will also increase generation time.",
            value=False,
        )
        speed_slider = gr.Slider(
            label="Speed",
            minimum=0.3,
            maximum=2.0,
            value=1.0,
            step=0.1,
            info="Adjust the speed of the audio.",
        )
        cross_fade_duration_slider = gr.Slider(
            label="Cross-Fade Duration (s)",
            minimum=0.0,
            maximum=1.0,
            value=0.15,
            step=0.01,
            info="Set the duration of the cross-fade between audio clips.",
        )

    audio_output = gr.Audio(label="Synthesized Audio")
    spectrogram_output = gr.Image(label="Spectrogram")

    generate_btn.click(
        infer,
        inputs=[
            ref_audio_input,
            ref_text_input,
            gen_text_input,
            model_choice,
            remove_silence,
            cross_fade_duration_slider,
            speed_slider,
        ],
        outputs=[audio_output, spectrogram_output],
    )


def parse_speechtypes_text(gen_text):
    # Pattern to find {speechtype}
    pattern = r"\{(.*?)\}"

    # Split the text by the pattern
    tokens = re.split(pattern, gen_text)

    segments = []

    current_emotion = "Regular"

    for i in range(len(tokens)):
        if i % 2 == 0:
            # This is text
            text = tokens[i].strip()
            if text:
                segments.append({"emotion": current_emotion, "text": text})
        else:
            # This is emotion
            emotion = tokens[i].strip()
            current_emotion = emotion

    return segments


with gr.Blocks() as app_multistyle:
    # New section for emotional generation
    gr.Markdown(
        """
    # Multiple Speech-Type Generation

    This section allows you to generate multiple speech types or multiple people's voices. Enter your text in the format shown below, and the system will generate speech using the appropriate type. If unspecified, the model will use the regular speech type. The current speech type will be used until the next speech type is specified.
    """
    )

    with gr.Row():
        gr.Markdown(
            """
            **Example Input:**                                                                      
            {Regular} Hello, I'd like to order a sandwich please.                                                         
            {Surprised} What do you mean you're out of bread?                                                                      
            {Sad} I really wanted a sandwich though...                                                              
            {Angry} You know what, darn you and your little shop!                                                                       
            {Whisper} I'll just go back home and cry now.                                                                           
            {Shouting} Why me?!                                                                         
            """
        )

        gr.Markdown(
            """
            **Example Input 2:**                                                                                
            {Speaker1_Happy} Hello, I'd like to order a sandwich please.                                                            
            {Speaker2_Regular} Sorry, we're out of bread.                                                                                
            {Speaker1_Sad} I really wanted a sandwich though...                                                                             
            {Speaker2_Whisper} I'll give you the last one I was hiding.                                                                     
            """
        )

    gr.Markdown(
        "Upload different audio clips for each speech type. The first speech type is mandatory. You can add additional speech types by clicking the 'Add Speech Type' button."
    )

    # Regular speech type (mandatory)
    with gr.Row():
        regular_name = gr.Textbox(value="Regular", label="Speech Type Name")
        regular_audio = gr.Audio(label="Regular Reference Audio", type="filepath")
        regular_ref_text = gr.Textbox(label="Reference Text (Regular)", lines=2)

    # Additional speech types (up to 99 more)
    max_speech_types = 100
    speech_type_rows = []
    speech_type_names = []
    speech_type_audios = []
    speech_type_ref_texts = []
    speech_type_delete_btns = []

    for i in range(max_speech_types - 1):
        with gr.Row(visible=False) as row:
            with gr.Column():
                name_input = gr.Textbox(label="Speech Type Name")
                delete_btn = gr.Button("Delete", variant="secondary")
            audio_input = gr.Audio(label="Reference Audio", type="filepath")
            ref_text_input = gr.Textbox(label="Reference Text", lines=2)
        speech_type_rows.append(row)
        speech_type_names.append(name_input)
        speech_type_audios.append(audio_input)
        speech_type_ref_texts.append(ref_text_input)
        speech_type_delete_btns.append(delete_btn)

    # Button to add speech type
    add_speech_type_btn = gr.Button("Add Speech Type")

    # Keep track of current number of speech types
    speech_type_count = gr.State(value=0)

    # Function to add a speech type
    def add_speech_type_fn(speech_type_count):
        if speech_type_count < max_speech_types - 1:
            speech_type_count += 1
            # Prepare updates for the rows
            row_updates = []
            for i in range(max_speech_types - 1):
                if i < speech_type_count:
                    row_updates.append(gr.update(visible=True))
                else:
                    row_updates.append(gr.update())
        else:
            # Optionally, show a warning
            row_updates = [gr.update() for _ in range(max_speech_types - 1)]
        return [speech_type_count] + row_updates

    add_speech_type_btn.click(
        add_speech_type_fn, inputs=speech_type_count, outputs=[speech_type_count] + speech_type_rows
    )

    # Function to delete a speech type
    def make_delete_speech_type_fn(index):
        def delete_speech_type_fn(speech_type_count):
            # Prepare updates
            row_updates = []

            for i in range(max_speech_types - 1):
                if i == index:
                    row_updates.append(gr.update(visible=False))
                else:
                    row_updates.append(gr.update())

            speech_type_count = max(0, speech_type_count - 1)

            return [speech_type_count] + row_updates

        return delete_speech_type_fn

    # Update delete button clicks
    for i, delete_btn in enumerate(speech_type_delete_btns):
        delete_fn = make_delete_speech_type_fn(i)
        delete_btn.click(delete_fn, inputs=speech_type_count, outputs=[speech_type_count] + speech_type_rows)

    # Text input for the prompt
    gen_text_input_emotional = gr.Textbox(
        label="Text to Generate",
        lines=10,
        placeholder="Enter the script with speaker names (or emotion types) at the start of each block, e.g.:\n\n{Regular} Hello, I'd like to order a sandwich please.\n{Surprised} What do you mean you're out of bread?\n{Sad} I really wanted a sandwich though...\n{Angry} You know what, darn you and your little shop!\n{Whisper} I'll just go back home and cry now.\n{Shouting} Why me?!",
    )

    # Model choice
    model_choice_emotional = gr.Radio(choices=["F5-TTS", "E2-TTS"], label="Choose TTS Model", value="F5-TTS")

    with gr.Accordion("Advanced Settings", open=False):
        remove_silence_emotional = gr.Checkbox(
            label="Remove Silences",
            value=False,
        )

    # Generate button
    generate_emotional_btn = gr.Button("Generate Emotional Speech", variant="primary")

    # Output audio
    audio_output_emotional = gr.Audio(label="Synthesized Audio")

    @gpu_decorator
    def generate_emotional_speech(
        regular_audio,
        regular_ref_text,
        gen_text,
        *args,
    ):
        num_additional_speech_types = max_speech_types - 1
        speech_type_names_list = args[:num_additional_speech_types]
        speech_type_audios_list = args[num_additional_speech_types : 2 * num_additional_speech_types]
        speech_type_ref_texts_list = args[2 * num_additional_speech_types : 3 * num_additional_speech_types]
        model_choice = args[3 * num_additional_speech_types]
        remove_silence = args[3 * num_additional_speech_types + 1]

        # Collect the speech types and their audios into a dict
        speech_types = {"Regular": {"audio": regular_audio, "ref_text": regular_ref_text}}

        for name_input, audio_input, ref_text_input in zip(
            speech_type_names_list, speech_type_audios_list, speech_type_ref_texts_list
        ):
            if name_input and audio_input:
                speech_types[name_input] = {"audio": audio_input, "ref_text": ref_text_input}

        # Parse the gen_text into segments
        segments = parse_speechtypes_text(gen_text)

        # For each segment, generate speech
        generated_audio_segments = []
        current_emotion = "Regular"

        for segment in segments:
            emotion = segment["emotion"]
            text = segment["text"]

            if emotion in speech_types:
                current_emotion = emotion
            else:
                # If emotion not available, default to Regular
                current_emotion = "Regular"

            ref_audio = speech_types[current_emotion]["audio"]
            ref_text = speech_types[current_emotion].get("ref_text", "")

            # Generate speech for this segment
            audio, _ = infer(ref_audio, ref_text, text, model_choice, remove_silence, 0)
            sr, audio_data = audio

            generated_audio_segments.append(audio_data)

        # Concatenate all audio segments
        if generated_audio_segments:
            final_audio_data = np.concatenate(generated_audio_segments)
            return (sr, final_audio_data)
        else:
            gr.Warning("No audio generated.")
            return None

    generate_emotional_btn.click(
        generate_emotional_speech,
        inputs=[
            regular_audio,
            regular_ref_text,
            gen_text_input_emotional,
        ]
        + speech_type_names
        + speech_type_audios
        + speech_type_ref_texts
        + [
            model_choice_emotional,
            remove_silence_emotional,
        ],
        outputs=audio_output_emotional,
    )

    # Validation function to disable Generate button if speech types are missing
    def validate_speech_types(gen_text, regular_name, *args):
        num_additional_speech_types = max_speech_types - 1
        speech_type_names_list = args[:num_additional_speech_types]

        # Collect the speech types names
        speech_types_available = set()
        if regular_name:
            speech_types_available.add(regular_name)
        for name_input in speech_type_names_list:
            if name_input:
                speech_types_available.add(name_input)

        # Parse the gen_text to get the speech types used
        segments = parse_speechtypes_text(gen_text)
        speech_types_in_text = set(segment["emotion"] for segment in segments)

        # Check if all speech types in text are available
        missing_speech_types = speech_types_in_text - speech_types_available

        if missing_speech_types:
            # Disable the generate button
            return gr.update(interactive=False)
        else:
            # Enable the generate button
            return gr.update(interactive=True)

    gen_text_input_emotional.change(
        validate_speech_types,
        inputs=[gen_text_input_emotional, regular_name] + speech_type_names,
        outputs=generate_emotional_btn,
    )


with gr.Blocks() as app_chat:
    gr.Markdown(
        """
# Voice Chat
Have a conversation with an AI using your reference voice! 
1. Upload a reference audio clip and optionally its transcript.
2. Load the chat model.
3. Record your message through your microphone.
4. The AI will respond using the reference voice.
"""
    )

    load_chat_model_btn = gr.Button("Load Chat Model", variant="primary")

    chat_interface_container = gr.Column(visible=False)

    @gpu_decorator
    def load_chat_model():
        global chat_model_state, chat_tokenizer_state
        if chat_model_state is None:
            show_info = gr.Info
            show_info("Loading chat model...")
            model_name = "Qwen/Qwen2.5-3B-Instruct"
            chat_model_state = AutoModelForCausalLM.from_pretrained(model_name, torch_dtype="auto", device_map="auto")
            chat_tokenizer_state = AutoTokenizer.from_pretrained(model_name)
            show_info("Chat model loaded.")

        return gr.update(visible=False), gr.update(visible=True)

    load_chat_model_btn.click(load_chat_model, outputs=[load_chat_model_btn, chat_interface_container])

    with chat_interface_container:
        with gr.Row():
            with gr.Column():
                ref_audio_chat = gr.Audio(label="Reference Audio", type="filepath")
            with gr.Column():
                with gr.Accordion("Advanced Settings", open=False):
                    model_choice_chat = gr.Radio(
                        choices=["F5-TTS", "E2-TTS"],
                        label="TTS Model",
                        value="F5-TTS",
                    )
                    remove_silence_chat = gr.Checkbox(
                        label="Remove Silences",
                        value=True,
                    )
                    ref_text_chat = gr.Textbox(
                        label="Reference Text",
                        info="Optional: Leave blank to auto-transcribe",
                        lines=2,
                    )
                    system_prompt_chat = gr.Textbox(
                        label="System Prompt",
                        value="You are not an AI assistant, you are whoever the user says you are. You must stay in character. Keep your responses concise since they will be spoken out loud.",
                        lines=2,
                    )

        chatbot_interface = gr.Chatbot(label="Conversation")

        with gr.Row():
            with gr.Column():
                audio_output_chat = gr.Audio(autoplay=True)
            with gr.Column():
                audio_input_chat = gr.Microphone(
                    label="Speak your message",
                    type="filepath",
                )

        clear_btn_chat = gr.Button("Clear Conversation")

        conversation_state = gr.State(
            value=[
                {
                    "role": "system",
                    "content": "You are not an AI assistant, you are whoever the user says you are. You must stay in character. Keep your responses concise since they will be spoken out loud.",
                }
            ]
        )

        # Modify process_audio_input to use model and tokenizer from state
        @gpu_decorator
        def process_audio_input(audio_path, history, conv_state):
            """Handle audio input from user"""
            if not audio_path:
                return history, conv_state, ""

            text = ""
            text = preprocess_ref_audio_text(audio_path, text)[1]

            if not text.strip():
                return history, conv_state, ""

            conv_state.append({"role": "user", "content": text})
            history.append((text, None))

            response = generate_response(conv_state, chat_model_state, chat_tokenizer_state)

            conv_state.append({"role": "assistant", "content": response})
            history[-1] = (text, response)

            return history, conv_state, ""

        @gpu_decorator
        def generate_audio_response(history, ref_audio, ref_text, model, remove_silence):
            """Generate TTS audio for AI response"""
            if not history or not ref_audio:
                return None

            last_user_message, last_ai_response = history[-1]
            if not last_ai_response:
                return None

            audio_result, _ = infer(
                ref_audio,
                ref_text,
                last_ai_response,
                model,
                remove_silence,
                cross_fade_duration=0.15,
                speed=1.0,
            )
            return audio_result

        def clear_conversation():
            """Reset the conversation"""
            return [], [
                {
                    "role": "system",
                    "content": "You are not an AI assistant, you are whoever the user says you are. You must stay in character. Keep your responses concise since they will be spoken out loud.",
                }
            ]

        def update_system_prompt(new_prompt):
            """Update the system prompt and reset the conversation"""
            new_conv_state = [{"role": "system", "content": new_prompt}]
            return [], new_conv_state

        # Handle audio input
        audio_input_chat.stop_recording(
            process_audio_input,
            inputs=[audio_input_chat, chatbot_interface, conversation_state],
            outputs=[chatbot_interface, conversation_state],
        ).then(
            generate_audio_response,
            inputs=[chatbot_interface, ref_audio_chat, ref_text_chat, model_choice_chat, remove_silence_chat],
            outputs=audio_output_chat,
        )

        # Handle clear button
        clear_btn_chat.click(
            clear_conversation,
            outputs=[chatbot_interface, conversation_state],
        )

        # Handle system prompt change and reset conversation
        system_prompt_chat.change(
            update_system_prompt,
            inputs=system_prompt_chat,
            outputs=[chatbot_interface, conversation_state],
        )


with gr.Blocks() as app:
    gr.Markdown(
        """
# E2/F5 TTS

This is a local web UI for F5 TTS with advanced batch processing support. This app supports the following TTS models:

* [F5-TTS](https://arxiv.org/abs/2410.06885) (A Fairytaler that Fakes Fluent and Faithful Speech with Flow Matching)
* [E2 TTS](https://arxiv.org/abs/2406.18009) (Embarrassingly Easy Fully Non-Autoregressive Zero-Shot TTS)

The checkpoints support English and Chinese.

If you're having issues, try converting your reference audio to WAV or MP3, clipping it to 15s, and shortening your prompt.

**NOTE: Reference text will be automatically transcribed with Whisper if not provided. For best results, keep your reference clips short (<15s). Ensure the audio is fully uploaded before generating.**
"""
    )
    gr.TabbedInterface(
        [app_tts, app_multistyle, app_chat, app_credits],
        ["TTS", "Multi-Speech", "Voice-Chat", "Credits"],
    )


@click.command()
@click.option("--port", "-p", default=None, type=int, help="Port to run the app on")
@click.option("--host", "-H", default=None, help="Host to run the app on")
@click.option(
    "--share",
    "-s",
    default=False,
    is_flag=True,
    help="Share the app via Gradio share link",
)
@click.option("--api", "-a", default=True, is_flag=True, help="Allow API access")
def main(port, host, share, api):
    global app
    print("Starting app...")
    app.queue(api_open=api).launch(server_name=host, server_port=port, share=share, show_api=api)


if __name__ == "__main__":
    if not USING_SPACES:
        main()
    else:
        app.queue().launch()