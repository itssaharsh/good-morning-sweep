# The sweep against a person's own clean-up of the same phone

Same phone, same night. The person deleted by hand first; the sweep then looked at the pictures they deleted (from the gallery's Recently deleted bin) and the ones they kept. The model and its prompt were the same for both sets, and each set was scanned once. The rules changed once, after reading the kept set's suggestions (the first version ticked receipts, an ID notice and study questions there), so the kept set's rule numbers are after fixes found on that set. The deleted-by-hand set was scored after that, once, with the final rules.

| | Pictures | Gemma alone would delete | Gemma + rules, ticked | Gemma + rules, shown at all |
|---|---|---|---|---|
| Deleted by hand | 823 | 94 (11%) | 6 (1%) | 21 (3%) |
| Kept by hand | 1124 | 207 (18%) | 5 (0%) | 50 (4%) |

Kinds Gemma gave the pictures deleted by hand: {'photo': 554, 'screenshot': 88, 'document': 57, 'ad': 42, 'other': 17, 'accidental': 14, 'quote': 13, 'unreadable': 13, 'meme': 12, 'news': 10, 'greeting': 3}

Kinds Gemma gave the pictures kept: {'photo': 464, 'document': 208, 'screenshot': 161, 'ad': 108, 'other': 45, 'quote': 40, 'unreadable': 39, 'meme': 17, 'news': 16, 'greeting': 13, 'accidental': 13}

Among the kept pictures, every ticked suggestion is either junk the person missed or a picture they want; those are labelled by the person (`sweep label`) and counted in `sweep report`.

Gemma's time per picture: 5.0 s on average over 1947 answers (laptop CPU, no GPU).
