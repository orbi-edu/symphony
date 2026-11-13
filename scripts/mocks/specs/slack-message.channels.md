---
title: "message.channels event"
summary: "A message was posted to a channel"
url: "https://docs.slack.dev/reference/events/message.channels"
type: "reference"
subtype: "event"
event_name: "message.channels"
scopes:
  - "channels:history"
apis:
  - "Events"
---

Source: https://docs.slack.dev/reference/events/message.channels

# message.channels event

### A message was posted to a channel

## Facts

**Required Scopes**

[`channels:history`](https://docs.slack.dev/reference/scopes/channels.history.md)

**Compatible APIs**

[`Events`](https://docs.slack.dev/apis/events-api.md)

## Usage info {#usage-info}

This example includes both the [event wrapper](https://docs.slack.dev/apis/events-api.md#callback-field) and the [event structure](https://docs.slack.dev/apis/events-api.md#event-type-structure) for the `message.channels` event.

```
{    "token": "one-long-verification-token",    "team_id": "T123ABC456",    "api_app_id": "A123ABC456",    "event": {        "type": "message",        "channel": "C123ABC456",        "user": "U123ABC456",        "text": "Live long and prospect.",        "ts": "1355517523.000005",        "event_ts": "1355517523.000005",        "channel_type": "channel"    },    "type": "event_callback",    "authorizations": [        {            "team_id": "T123ABC456",            "user_id": "U123ABC456",            "is_bot": false,            "is_enterprise_install": false,        }    ],    "event_id": "Ev123ABC456",    "event_time": 1355517523}
```

The semantics for this message event type are similar to the core [message](https://docs.slack.dev/reference/events/message.md) event sent through the RTM API. Messages often include many more fields than those shown above.

Differentiate public channel messages from other `message.*` events by looking for the `event`'s `channel_type` field set to `"channel"`.

To receive only messages sent to your app, subscribe to [`app_mention`](https://docs.slack.dev/reference/events/app_mention.md) events instead.
