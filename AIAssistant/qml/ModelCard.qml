// Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
// Card of a model-tree node, drawn by the slicer left of the chat over the build plate view:
// the chat page cannot draw outside its window, and inside it the card would cover other nodes.
// window.py (HoverCard) parents it to the main window's content item and fills the properties.
import QtQuick 2.7

Item
{
    id: card

    property int rightInset: 0     // width taken by the chat strip at the right edge
    property real cardY: 0.5       // centre of the hovered node, as a fraction of the window height
    property string title: ""
    property string stats: ""
    property var rows: []          // prepared rich-text lines: label, old value struck out, new value bold
    property string emptyText: ""

    anchors.fill: parent
    visible: false
    z: 1001

    Rectangle
    {
        id: box
        width: 270
        height: column.height + 24
        x: card.width - card.rightInset - width - 12
        y: Math.max(8, Math.min(card.height - height - 8, card.cardY * card.height - 24))
        color: "#ffffff"
        border.color: "#dadce0"
        border.width: 1
        radius: 10

        Column
        {
            id: column
            x: 12
            y: 12
            width: box.width - 24
            spacing: 5

            Text
            {
                width: column.width
                wrapMode: Text.WordWrap
                text: card.title
                color: "#111111"
                font.pixelSize: 13
                font.bold: true
            }
            Text
            {
                width: column.width
                wrapMode: Text.WordWrap
                visible: text !== ""
                text: card.stats
                color: "#5f6368"
                font.pixelSize: 12
            }
            Rectangle
            {
                width: column.width
                height: 1
                color: "#e8eaed"
                visible: card.rows.length > 0 || card.emptyText !== ""
            }
            Repeater
            {
                model: card.rows
                Text
                {
                    width: column.width
                    wrapMode: Text.WordWrap
                    textFormat: Text.RichText
                    text: modelData
                    color: "#111111"
                    font.pixelSize: 12
                }
            }
            Text
            {
                width: column.width
                wrapMode: Text.WordWrap
                visible: card.rows.length === 0 && text !== ""
                text: card.emptyText
                color: "#5f6368"
                font.pixelSize: 12
            }
        }
    }
}
