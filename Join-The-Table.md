# Join The Table

A D&D game that runs on my laptop. An AI narrates as DM — **out loud** — and you
play your own character from your phone or your computer.

Nothing is on the public internet. You connect to my machine over a private
network, which is why there are two steps instead of one.

---

## 1. Install Tailscale

This is the private network. Free, and you don't need to set anything up in
advance — my invite handles it.

- **Phone:** search "Tailscale" in the App Store or Play Store
- **Computer:** <https://tailscale.com/download>

## 2. Accept my invite

I'll send you a link. Open it, sign in (Google, GitHub, or email), and let
Tailscale connect.

You'll know it worked when the Tailscale app says **Connected**.

## 3. Open the game

> ### http://<this-machine>.<your-tailnet>.ts.net:5050

Any browser. Works fine on a phone.

## 4. Join

It asks for your name and a code.

> ### Join code: `R8Z73H`
>
> Not case sensitive. Spaces and dashes are ignored — `r8z-73h` works.

Then build a character: pick a race and class, roll your stats, and you're at
the table.

---

## Two things that will confuse you otherwise

### The DM talks, but your phone will block it at first

Phones refuse to play audio until you tap something. If you see a button
saying **TAP TO ENABLE THE DM'S VOICE**, tap it. You won't hear anything until
you do.

Nothing is lost while it's waiting — whatever the DM said plays once you allow
it.

There's also a **THIS DEVICE** toggle to mute just your end — stepping away,
taking a call — without silencing the DM for everyone else.

### There's a wait after you act

The DM's brain and its voice both run on one old laptop with no graphics card.
After you submit an action, expect **two to three minutes** before the narration
arrives, and another twenty to forty seconds before you hear it read aloud.

It varies, and a slow one is not a stuck one. It's thinking, not broken.

Everything else is instant — dice, hit points, conditions, whose turn it is.
Only the storytelling is slow.

---

## How a turn actually goes

1. You type what your character does, in plain English. *"I creep along the wall
   and try to pick the lock on the iron door."*
2. The DM decides what that needs and the **engine rolls it for real** — the AI
   isn't allowed to make up a number. You'll see the roll appear:
   `Ferrick rolls DEX: 18+6=24`
3. The DM narrates what happened, built on that actual result.
4. You hear it read aloud.

Everyone at the table sees every roll as it happens.

---

## If it doesn't work

- Does the Tailscale app say **Connected**?
- My laptop has to be switched on and running the game — the address only works
  while I'm hosting.
- Still stuck? Message me. I can see what the server is doing.
