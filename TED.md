# Ted

**A health buddy that lives in WhatsApp.** Tell it what you ate and how you moved, by text, photo or voice note, and it gives you one useful thing you can still do today. About 100 people have used it. Live at [heyted.in](https://heyted.in).

I built it alone, at night, with Claude. I am not an engineer. I am a GTM leader who builds around problems I have lived.

Ted grew out of **Nudge**, the Telegram version in this repo ([README.md](README.md)). Ted's own code stays private because it holds real people's conversations. This page explains how it works and what I learned.

## How Ted works

| Part | What it does |
| --- | --- |
| **The gateway** | Holds the WhatsApp conversation. Ted's personality lives here. |
| **The memory** | A database (Convex) of meals, water, steps, workouts, goals and facts people have shared. |
| **The safety gates** | A plugin the gateway loads. Messages in and replies out pass through it. This is the most important code in the project. |

## The safety gates

A health bot that sounds confident and is wrong can hurt someone. So the gates check every reply before it goes out.

* **Under 18s.** Anyone who says they are under 18 never gets calorie targets. Their age is stored so it survives restarts, and it can't be lowered later.
* **Calorie numbers.** Ted can't invent a target or a deficit. Any number near words like deficit, maintenance or target is checked. Real targets come from a standard formula (Mifflin St Jeor) using the person's own height, weight and age.
* **Be sure, or ask.** If someone answers "63 or 64, not sure", Ted asks again instead of quietly storing 63.
* **Delete my data.** Saying it removes what Ted holds about that person.

Gates only help if they are running. The gateway quietly skips a plugin that fails to load and keeps chatting, unchecked. So a separate check runs after every restart to prove the gates are loaded.

## What building it taught me

* **Test the whole path, not just the rule.** A rule can be correct and still never run, if the information it needs arrives too late or the message takes a different route. Several of my worst bugs were rules that were right but unreachable.
* **Lists of phrases leak.** Blocking known bad phrases fails on the first new wording. Checking for the pattern (a number near a calorie word) holds up.
* **Every message needs the same checks.** Scheduled reminders once took a different route from normal replies. Now everything Ted sends goes through the same gates, whatever triggered it.
* **Users will tell you what's wrong.** Photo logs were corrected far more often than typed ones, which told me where to spend my time.

## Measuring it

* **Evals.** 50 tricky test conversations, covering minors, made-up numbers, Hinglish and corrections, run against changes.
* **Cost per user.** Spend is tracked per person each month. WhatsApp charges per message, so one extra reply bubble a day costs about ₹33 per user per month. Ted is designed to say it in one message.
* **Reported replies.** People can flag a wrong reply, and each one is stored for review.

## From Nudge to Ted

Nudge was the first try: Telegram, free tier services, sign up on a website first. People wanted the coaching, but not another app or a form. Ted moved to WhatsApp, where people already are, and onboarding happens inside the chat.

Built by [Vandana Agarwal Nik](https://vandanaagarwal.in), with Claude.
