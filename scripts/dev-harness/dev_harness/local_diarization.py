"""Independent offline speaker turns and word reconciliation for every STT adapter."""
from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
import json
import math
import os
from pathlib import Path
import sys
import time
import wave

MODEL = 'pyannote/speaker-diarization-community-1'
MODELS = {MODEL, 'pyannote/speaker-diarization-3.1'}


def reconcile(segments, turns):
    """Keep timed words intact and split display segments at actual speaker changes."""
    for start, end, label in turns:
        if not all(math.isfinite(v) for v in (start, end)) or start < 0 or end <= start or not isinstance(label, str):
            raise ValueError('Invalid speaker turns')
    turns = sorted(turns)
    starts, max_ends = [], []
    for start, end, _ in turns:
        starts.append(start)
        max_ends.append(max(end, max_ends[-1] if max_ends else end))
    def speaker(item):
        scores = {}
        left = bisect_right(max_ends, item['start'])
        right = bisect_left(starts, item['end'])
        for start, end, label in turns[left:right]:
            overlap = max(0, min(item['end'], end) - max(item['start'], start))
            if overlap:
                scores[label] = scores.get(label, 0) + overlap
        return max(scores, key=scores.get) if scores else None
    output = []
    for segment in segments:
        words = segment.get('words') or []
        if not words:
            output.append({**segment, 'speaker': speaker(segment)})
            continue
        if any(word.get('start') is None or word.get('end') is None for word in words):
            # Alignment can leave names or punctuation untimed. Keep the whole
            # sentence rather than splitting it and losing unaligned text.
            label = speaker(segment)
            output.append({**segment, 'speaker': label,
                           'words': [{**word, 'speaker': label} for word in words]})
            continue
        labels = [speaker(word) for word in words]
        neighbour = next((label for word, label in zip(words, labels) if word['end'] > word['start']),
                         speaker(segment))
        for index, word in enumerate(words):
            # Whisper emits zero-length fillers that overlap no turn. They cannot
            # own a speaker segment, so they stay with the adjacent timed word,
            # including its unknown speaker. Skipping unknown words could create
            # an isolated zero-length group with an earlier sentence timestamp.
            if word['start'] == word['end']:
                labels[index] = neighbour
            else:
                neighbour = labels[index]
        groups = []
        for word, label in zip(words, labels):
            word = {**word, 'speaker': label}
            if not groups or groups[-1][-1]['speaker'] != label:
                groups.append([])
            groups[-1].append(word)
        for group in groups:
            text = ''.join(w['word'] if w['word'].startswith(' ') else ' ' + w['word'] for w in group).strip()
            start, end = group[0]['start'], max(w['end'] for w in group)
            if end <= start:  # Only zero-length words: keep the sentence interval.
                start, end = segment['start'], segment['end']
            output.append({**segment, 'start': start, 'end': end,
                           'text': text, 'speaker': group[0]['speaker'], 'words': group})
    return output


def infer(audio, model, device='cpu', threads=4, num_speakers=None):
    import numpy as np
    import torch
    from pyannote.audio import Pipeline
    torch.set_num_threads(threads)
    started = time.monotonic()
    pipeline = Pipeline.from_pretrained(model)
    pipeline.to(torch.device(device))
    with wave.open(str(audio)) as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16000):
            raise ValueError('Expected mono PCM16 16kHz')
        waveform = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2').astype(np.float32) / 32768
    result = pipeline({'waveform': torch.from_numpy(waveform).unsqueeze(0), 'sample_rate': 16000},
                      **({'num_speakers': num_speakers} if num_speakers else {}))
    annotation = result.exclusive_speaker_diarization
    turns = [(float(t.start), float(t.end), label)
             for t, _, label in annotation.itertracks(yield_label=True)]
    return {'turns': turns, 'seconds': round(time.monotonic() - started, 3), 'device': device,
            'speakers': len({t[2] for t in turns})}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--audio', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--device', choices=['cpu', 'mps'], default='cpu')
    parser.add_argument('--threads', type=int, default=4)
    parser.add_argument('--num-speakers', type=int)
    args = parser.parse_args()
    os.umask(0o077)
    os.environ.setdefault("MPLCONFIGDIR", str(Path(sys.executable).parent.parent.parent / "matplotlib"))
    report = os.fdopen(os.dup(1), 'w')
    with open(os.devnull, 'w') as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    def no_network(event, _args):
        if event in {'socket.connect', 'socket.getaddrinfo'}:
            raise RuntimeError('Offline inference only')
    sys.addaudithook(no_network)
    try:
        result = infer(args.audio, args.model, args.device, args.threads, args.num_speakers)
        with Path(args.output).open('x') as stream:
            json.dump(result, stream, allow_nan=False)
        report.write(json.dumps({k: v for k, v in result.items() if k != 'turns'}))
        return 0
    except Exception as error:
        report.write(json.dumps({'status': 'failed', 'error_type': type(error).__name__}))
        return 1
    finally:
        report.flush()


if __name__ == '__main__':
    sys.exit(main())
