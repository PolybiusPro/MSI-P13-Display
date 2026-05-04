# Заметки по touch-протоколу

Тачскрин виден как HID interface `3` на том же USB-устройстве.

## Разбор report

```mermaid
flowchart TD
    R[Raw HID report] --> ID{Report ID}
    ID -->|0x01| A[status byte 1, x/y в bytes 3..6]
    ID -->|0x54| B[status byte 2, x/y в bytes 4..7]
    A --> P[pressed = Tip Switch and contact_count > 0]
    B --> P
    P --> S[scale raw 0..4095 to display pixels]
    S --> F[logical state filter]
```

Наблюдавшиеся status bits:

```text
bit 0  Tip Switch
bit 1  In Range
```

Масштабирование координат:

```python
x = round(x_raw * (width - 1) / 4095)
y = round(y_raw * (height - 1) / 4095)
```

## Logical Filter

Контроллер может продолжать присылать pressed contact с неизменными координатами
после отпускания пальца. Поэтому reader генерирует логические события:

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

Реализация находится в `src/em3499_monitor/touch.py`.
