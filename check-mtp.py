p = r"D:\model\unsloth\Qwen3.8-27B-GGUF\Qwen3.8-27B-UD-IQ4_XS.gguf"

keys = [
    b"nextn_predict_layers",
    b"qwen35.nextn_predict_layers",
    b"qwen3next.nextn_predict_layers",
]

found = set()
tail = b""

with open(p, "rb") as f:
    while True:
        chunk = f.read(16 * 1024 * 1024)
        if not chunk:
            break

        data = tail + chunk

        for key in keys:
            if key in data:
                found.add(key.decode())

        if len(data) > 256:
            tail = data[-256:]

for x in sorted(found):
    print(x)

if not found:
    print("NO_MTP_METADATA_FOUND")
