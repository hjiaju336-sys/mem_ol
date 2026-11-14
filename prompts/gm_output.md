# GM 输出格式

> 追加在 GM 用户消息末尾，约束返回结构；改这里不需要改代码。

## System

【输出格式】只输出一个 JSON 对象：

```json
{
  "plan": {
    "intent": "",
    "expected_outcome": "",
    "continuity": "",
    "state_change": ""
  },
  "effects": [
    {
      "kind": "tag_add | tag_remove | flag_set | scale_delta | hp_delta | time_advance",
      "target": "player 或对象名",
      "field": "scale/flag 名",
      "value": 0,
      "reason": ""
    }
  ],
  "reply": "故事正文"
}
```

【effects 规则】

- 只写正文里新产生、且【本轮结算】没有覆盖的后果。
- 不要重复结算已经扣过的 HP；重复的 hp_delta 会被拒绝。
- `tag_add`/`tag_remove`：新状态、伤势、被跟踪等，`value` 填标签名。
- `scale_delta`：警戒/怀疑/疲劳等 0~100 的数值，`target` 填对象，`field` 填字段名。
- `flag_set`：世界状态，`field` 填标记名，`value` 填 true/false。
- `time_advance`：剧情明确发生时间跳跃时使用；`value` 填 `{"days":0,"hours":0,"minutes":0}`，引擎按历法推进时间。不要在正文里写“三天后”却遗漏这个效果。
- 没有剧情后果就输出空数组 `[]`。

【其他】

- `plan` 只用于自检，reply 中不得出现 plan、规则或本条说明。
- `reply` 只写故事正文。
