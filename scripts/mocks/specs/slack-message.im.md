---
title: "message.im event"
summary: "A message was posted in a direct message channel"
url: "https://docs.slack.dev/reference/events/message.im"
type: "reference"
subtype: "event"
event_name: "message.im"
scopes:
  - "im:history"
apis:
  - "Events"
---

Source: https://docs.slack.dev/reference/events/message.im

# message.im event

### A message was posted in a direct message channel

## Facts

**Required Scopes**

[`im:history`](https://docs.slack.dev/reference/scopes/im.history.md)

**Compatible APIs**

[`Events`](https://docs.slack.dev/apis/events-api.md)

## Usage info {#usage-info}

This example includes both the [event wrapper](https://docs.slack.dev/apis/events-api.md#callback-field) and the [event structure](https://docs.slack.dev/apis/events-api.md#event-type-structure) for the `message.im` event.

```
{    "token": "one-long-verification-token",    "team_id": "T123ABC456",    "api_app_id": "A0PNCHHK2",    "event": {        "type": "message",        "channel": "D024BE91L",        "user": "U2147483697",        "text": "Hello hello can you hear me?",        "ts": "1355517523.000005",        "event_ts": "1355517523.000005",        "channel_type": "im"    },    "type": "event_callback",    "authorizations": [        {            "team_id": "T123ABC456",            "user_id": "U2147483697",            "is_bot": false,            "is_enterprise_install": false,        }    ],    "event_id": "Ev0PV52K21",    "event_time": 1355517523}
```

The semantics for this message event type are similar to the core [message](https://docs.slack.dev/reference/events/message.md) event sent through the RTM API. Messages often include many more fields than those shown above.

Differentiate direct messages from other `message.*` events by looking for the `event`'s `channel_type` field set to `"im"`.
