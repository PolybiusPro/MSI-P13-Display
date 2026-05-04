# Touch Protocol Notes

The touch panel is exposed as HID interface `3` on the same USB device.

## Report Parsing

```mermaid
flowchart TD
    R[Raw HID report] --> ID{Report ID}
    ID -->|0x01| A[status byte 1, x/y at bytes 3..6]
    ID -->|0x54| B[status byte 2, x/y at bytes 4..7]
    A --> P[pressed = Tip Switch and contact_count > 0]
    B --> P
    P --> S[scale raw 0..4095 to display pixels]
    S --> F[logical state filter]
```

Observed status bits:

```text
bit 0  Tip Switch
bit 1  In Range
```

Coordinate scaling:

```python
x = round(x_raw * (width - 1) / 4095)
y = round(y_raw * (height - 1) / 4095)
```

## Logical Filter

The controller can keep reporting a pressed contact with unchanged coordinates
after the finger is released. The reader therefore emits logical events:

```mermaid
stateDiagram-v2
    [*] --> Idle
    Idle --> Down: pressed report
    Down --> Move: coordinate changes
    Move --> Move: coordinate changes
    Down --> Up: inactive report
    Move --> Up: inactive report
    Down --> Up: stale identical reports
    Move --> Up: stale identical reports
    Down --> Up: no-report gap
    Move --> Up: no-report gap
    Up --> Idle
```

See `src/em3499_monitor/touch.py` for the standalone implementation.
