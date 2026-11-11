pub const LOCALES:[&str;9]=["zh_CN","en_US","de_DE","es_ES","fr_FR","ja_JP","ko_KR","pt_BR","ru_RU"];
pub struct Message {pub id:&'static str,pub text:[&'static str;9]}
pub const MESSAGES:&[Message]=&[
    Message{id:"title",text:["Tesseract","Tesseract","Tesseract","Tesseract","Tesseract","Tesseract","Tesseract","Tesseract","Tesseract"]},
    Message{id:"tray_tier_line",text:["当前层级：{tier}","Current tier: {tier}","Aktuelle Stufe: {tier}","Nivel actual: {tier}","Niveau actuel\u{a0}: {tier}","現在の階層：{tier}","현재 계층: {tier}","Camada atual: {tier}","Текущий уровень: {tier}"]},
];
