## upload_score
Use "Add a score", then "Upload MusicXML…", to load another song. Only MusicXML is accepted:
.musicxml, .xml or compressed .mxl. PDF, image, MIDI, audio and notation-program files (such as
.mscz) are not accepted; export or save the score as MusicXML from the notation program first.

## tracks_and_mixing
The "Tracks & Stems" panel lists every sung take and every instrument track. Each has Mute, Solo
and a volume slider, so one part can be made louder or softer than another. Instrument tracks also
have a sound selector to change the instrument, and the "Instrumental Level" slider sets all
instruments together. Each sung take has a download button for its own audio.

## download_mix
"Download mix (real-time)" in the score player saves what the player plays, with the current Mute,
Solo and volume settings, as one WAV file. It plays the song through in real time, so keep the tab
open until it finishes; it works for scores up to 5 minutes long. To get several sung parts and the
instruments in one file, sing each part as its own take (or use "Sing All"), then download the mix.

## score_view
"Page view" and "Horizontal view" change how the score is shown, and "Zoom in" / "Zoom out" change
its size. The chat panel can be collapsed to give the score more room. "Download score" saves the
score as a MusicXML file, and "Print score" prints it.

## sing_all
"Sing All" (the button above the chat box) sings every vocal part of the score in one go, and adds
the instrumentals. It opens a card in the chat with the parts, their AI voices and the credit total.
"Customise" opens the choices: tick or untick each part, pick each part's AI voice, "Sing in"
("Lyrics", "Solfège" or "Both"), "Verse", "Repeats", "Mixes", "Language", and "Style notes" for how
the voices should sound. The total updates after each change. "Sing it" starts it. On the free plan
the button reads "Upgrade to sing"; with too few credits it offers "Top up". While it runs, the card
shows each step and has a "Stop" button; what isn't sung is refunded. If a part can't be prepared,
the card says "Your song needs a decision": choose which parts to sing ("Sing as it is" keeps a part
that was split differently than expected), or "Cancel and refund". With no answer in 24 hours it is
cancelled and refunded. If some takes fail, "Retry failed parts" sings just those again.

## sing_all_download
When a Sing All finishes, "Download zip" appears at the bottom of its card, after the credits
charged, and next to the zoom buttons above the score. The zip has every sung take and the mixes
chosen under "Mixes", plus a README with the song's info. If the tab is in the background when it
finishes, the tab plays a chime and its title is marked.

## share
The Share button above the score (or "Share" on a song in "My songs") makes a link anyone can open
to listen and follow the score. The first time, a notice explains what sharing means; choose
"Share" to accept. "Copy" copies the link. Listeners can play and pause, switch parts on and off,
switch between lyrics and solfège, zoom and change the view; they can't edit or download. Changes
made later show on the same link. "Turn off sharing" ends the link for good; sharing again makes a
new one. Share stays off until a part has been sung, and when sharing isn't available for the
account; its tooltip says why.

## song_info
The "Song info" button (ⓘ) by the song's title opens the song's details: "Title", "Subtitle",
"Composer", "Lyrics by", "Arranger", "Credits", "Score copyright", "Produced by" and "Description",
plus the "License" ("All rights reserved", "CC BY 4.0" or "Public domain (CC0)"). Fields come from
the score when it has them and can be edited; "Save" keeps them. The license covers the recording,
not the song itself. Voices are filled in from the sung parts. The info shows on the Share page and
in the Sing All zip's README.

## my_songs
"My songs" lists saved songs, most recent first. "Open" opens one; "Shared" marks a song with a
live link; "Share" and "Delete" are on each row. Uploading a score starts a new song.

## credits_and_plans
The "Upgrade" button at the top of the page opens the plans and the "Credit Pack", a one-off top-up
alongside any plan. The credits next to it show what's available; click them for the breakdown.
Subscribers manage or cancel their plan under "Billing" in the account menu. Each take's quote
shows its exact credits before it starts.

## credit_rates
| What | Credits | When it's charged |
|---|---|---|
| A sung take (lyrics or solfège) | 1 per {vocal_credit_seconds} seconds, rounded up | for each part, every time it's sung. Sing All with "Both" sings two takes per part |
| Instrumental accompaniment | 1 per {instrumental_credit_minutes} minutes, rounded up | once per uploaded score, however many instrument tracks it has |
| A Sing All mix file | {mix_credits_per_file} per file | for each mix chosen under "Mixes" |

Length follows the "With Repeats" setting, and the final charge uses the actual audio length.
Free: splitting a part into singable lines, "Download mix (real-time)", downloading takes and the
Sing All zip, sharing, and Song info. Credits for anything that isn't sung are refunded.
