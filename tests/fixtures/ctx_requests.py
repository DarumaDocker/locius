# The 10 everyday requests used to measure personal context (batch 2): request, regex a useful fact must match.
REQUESTS = [
    ("P01", "帮我在迪卡侬挑一双适合日常跑步的跑鞋，选好尺码加入购物车就行，先不用下单。", r"43码|运动鞋43"),
    ("P02", "帮我网上挑一个好看的手机壳，给我 3 个候选，先不用下单。", r"iPhone 17 Pro Max"),
    ("P03", "帮我找下个月从新加坡飞东京的机票，给我两三个选择，先不要订。", r"business class|商务舱"),
    ("P04", "帮我在东京找一家酒店，下个月住 3 晚，给几个选项，先不要订。", r"boutique|精品"),
    ("P05", "帮我找个这周末在新加坡的徒步路线。", r"medium-low|3 to 5"),
    ("P06", "这周六晚上想出去吃饭，帮我找两家餐厅看看有没有空位，先别订。", r"Hunan|Japanese|湘|日本"),
    ("P07", "帮我在迪卡侬买一件运动T恤，挑合适的尺码加入购物车，先不用下单。", r"身高177|177"),
    ("P08", "给 James 写封邮件约他下周二下午开会，先存草稿就行。", r"sending emails from|James"),
    ("P09", "帮我整理一份本周 AI 和机器人领域的新闻简报。", r"PDF|Chinese|中文"),
    ("P10", "帮我挑一支笔送朋友，给我 3 个推荐。", r"metal pens|金属"),
]
