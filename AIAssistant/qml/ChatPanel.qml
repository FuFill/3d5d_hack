// Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
// Strip on the right edge of the slicer window that holds the embedded chat window.
// window.py parents it to the main window's content item, keeps the slicer UI out of
// it and places the browser window over the area right of the resize handle.
import QtQuick 2.7

Item
{
    id: panel

    property int panelWidth: 400
    property int minimumWidth: 300
    property int maximumWidth: 900
    property int handleWidth: 6
    property string statusText: ""
    signal resized(int width)
    signal resizeFinished(int width)

    anchors.top: parent ? parent.top : undefined
    anchors.bottom: parent ? parent.bottom : undefined
    anchors.right: parent ? parent.right : undefined
    width: panelWidth + handleWidth
    z: 1000

    Rectangle
    {
        anchors.fill: parent
        color: "#f4f5f7"
        Text
        {
            anchors.centerIn: parent
            anchors.horizontalCenterOffset: panel.handleWidth / 2
            width: panel.panelWidth - 40
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
            color: "#5f6368"
            text: panel.statusText
        }
    }

    Rectangle
    {
        id: handle
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        width: panel.handleWidth
        color: dragArea.pressed || dragArea.containsMouse ? "#c3c7cf" : "#dadce0"

        MouseArea
        {
            id: dragArea
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.SizeHorCursor
            onPositionChanged:
            {
                if (!pressed || !panel.parent)
                {
                    return
                }
                var x = mapToItem(panel.parent, mouse.x, 0).x
                var width = panel.parent.width - x - panel.handleWidth / 2
                width = Math.max(panel.minimumWidth, Math.min(panel.maximumWidth, Math.round(width)))
                if (width != panel.panelWidth)
                {
                    panel.panelWidth = width
                    panel.resized(width)
                }
            }
            onReleased: panel.resizeFinished(panel.panelWidth)
        }
    }
}
