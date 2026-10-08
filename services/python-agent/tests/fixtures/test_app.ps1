# Self-contained target window for real-desktop tests (WinForms: native controls, full UI Automation).
# Tests must never operate on the user's own applications (Windows 11 Notepad opens files as tabs in the
# user's existing window).
#   powershell -NoProfile -STA -WindowStyle Hidden -File test_app.ps1   (hides its console, not the form) -Title <title> -SavePath <file>
# "Save" (File menu, the button or Ctrl+S) writes the text box content to -SavePath.
param([Parameter(Mandatory)][string]$Title, [Parameter(Mandatory)][string]$SavePath)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$form = New-Object System.Windows.Forms.Form
$form.Text = $Title
$form.Size = New-Object System.Drawing.Size(640, 360)
$form.StartPosition = 'Manual'
$form.Location = New-Object System.Drawing.Point(120, 120)
$form.Font = New-Object System.Drawing.Font('Segoe UI', 12)
$form.KeyPreview = $true

$editor = New-Object System.Windows.Forms.TextBox
$editor.Name = 'editor'
$editor.AccessibleName = 'Editor'
$editor.Location = New-Object System.Drawing.Point(16, 48)
$editor.Width = 590

$status = New-Object System.Windows.Forms.Label
$status.Name = 'status'
$status.AccessibleName = 'Status'
$status.Text = 'ready'
$status.Location = New-Object System.Drawing.Point(16, 140)
$status.AutoSize = $true

$save = {
    [System.IO.File]::WriteAllText($SavePath, $editor.Text, [System.Text.UTF8Encoding]::new($false))
    $status.Text = 'saved'
}

$button = New-Object System.Windows.Forms.Button
$button.Text = 'Save now'
$button.Location = New-Object System.Drawing.Point(16, 90)
$button.AutoSize = $true
$button.Add_Click($save)

$menu = New-Object System.Windows.Forms.MenuStrip
$file = New-Object System.Windows.Forms.ToolStripMenuItem('File')
foreach ($name in 'New', 'Open', 'Save', 'Exit') {
    $item = New-Object System.Windows.Forms.ToolStripMenuItem($name)
    switch ($name) {
        'New' { $item.Add_Click({ $editor.Clear() }) }
        'Save' { $item.Add_Click($save) }
        'Exit' { $item.Add_Click({ $form.Close() }) }
    }
    [void]$file.DropDownItems.Add($item)
}
[void]$menu.Items.Add($file)
$form.MainMenuStrip = $menu

$form.Add_KeyDown({ param($sender, $e) if ($e.Control -and $e.KeyCode -eq 'S') { & $save; $e.SuppressKeyPress = $true } })
$form.Controls.AddRange(@($editor, $button, $status, $menu))
[void]$form.ShowDialog()
