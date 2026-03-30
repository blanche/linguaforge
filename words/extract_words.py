import re

with open('words.html', 'r', encoding='utf-8') as f:
    content = f.read()

pattern = r'class="_blue wA">([^<]+)</span><span class="cCCC wT"> - ([^<]+)</span>'
pairs = re.findall(pattern, content)

with open('words.txt', 'w', encoding='utf-8') as f:
    for word, translation in pairs:
        first = translation.split(',')[0].strip()
        f.write(f"{word} = {first}\n")

print(f"Written {len(pairs)} pairs to words.txt")
