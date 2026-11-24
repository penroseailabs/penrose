; The Windows installer's data page: where penrose keeps its phone and files.
;
; electron-builder includes this file by name, for the Windows (NSIS) build only. The choice is saved to
; penrose\data-location.txt in the per-user app data folder, the one place every platform looks: desktop/src/main.js
; and backend/app/cli.py read it on Windows, macOS and Linux (here it is UTF-16LE without a BOM; they also take UTF-8).
; No file means the platform's default (%LOCALAPPDATA%\Relay here). Updates and existing installs never see this page.
; Silent installs can pass /DATADIR="D:\Some folder".
;
; It also makes penrose start at login (customInstall, customUnInstall below).

!ifndef BUILD_UNINSTALLER
  !include "nsDialogs.nsh"
  !include "LogicLib.nsh"

  !define DD_POINTER_DIR "$LOCALAPPDATA\penrose"
  !define DD_POINTER "$LOCALAPPDATA\penrose\data-location.txt"
  !define DD_FOLDER "penrose"
  !define DD_NEED_MB 12288     ; what setup and a typical phone use: about 12 GB
  !define DD_ROOMY_MB 20480    ; room for the phone to grow past a typical size
  !define DD_LIMIT_MB 65536    ; the phone's storage limit (disk.dataPartition.size in backend/app/provision)
  !define DD_MAX_PATH 100      ; the Android SDK nests deep inside this folder, and Windows paths stop at 260

  Var dd_Parent    ; what the user picked
  Var dd_Final     ; the data folder: the pick, plus a penrose folder inside it
  Var dd_Level     ; 0 fine, 1 caution, 2 too little space (asks before going on), 3 cannot be used
  Var dd_Message
  Var dd_FreeMB
  Var dd_Stale     ; an earlier choice that can no longer be found
  Var dd_Edit
  Var dd_FinalLabel
  Var dd_Status
!endif

!macro DD_REFUSE text
  StrCpy $dd_Level 3
  StrCpy $dd_Message "${text}"
  Return
!macroend

!macro DD_CAUTION text
  ${If} $dd_Level < 1
    StrCpy $dd_Level 1
  ${EndIf}
  StrCpy $dd_Message "$dd_Message$\r$\n${text}"
!macroend

!macro DD_REFUSE_CHAR char
  ${StrContains} $R1 `${char}` "$R0"
  ${If} $R1 != ""
    !insertmacro DD_REFUSE "A folder name cannot contain any of these: ? * < > | / : $\""
  ${EndIf}
!macroend

; The checks, apart from the page so they can be tested without a window.
!macro DataDirLogic
  ; In: $dd_Parent. Out: $dd_Final, $dd_Level, $dd_Message, $dd_FreeMB.
  Function DataDirEvaluate
    StrCpy $dd_Final ""
    StrCpy $dd_Level 0
    StrCpy $dd_Message ""
    StrCpy $dd_FreeMB ""

    StrCpy $R0 $dd_Parent
    trim:
      StrCpy $R1 $R0 1 -1
      StrCmp $R1 "\" 0 trimmed
      StrCpy $R0 $R0 -1
      Goto trim
    trimmed:

    ${If} $R0 == ""
      !insertmacro DD_REFUSE "Choose a folder."
    ${EndIf}
    StrCpy $R1 $R0 2
    ${If} $R1 == "\\"
    ${OrIf} $R1 == "//"
      !insertmacro DD_REFUSE "Network folders are too slow for the phone. Choose a folder on this computer."
    ${EndIf}
    StrCpy $R1 $R0 1 1
    StrCpy $R2 $R0 1 2
    ${If} $R1 != ":"
      !insertmacro DD_REFUSE "Enter a full path, such as D:\Apps."
    ${EndIf}
    ${If} $R2 != "\"
    ${AndIf} $R2 != ""
      !insertmacro DD_REFUSE "Enter a full path, such as D:\Apps."
    ${EndIf}
    !insertmacro DD_REFUSE_CHAR "?"
    !insertmacro DD_REFUSE_CHAR "*"
    !insertmacro DD_REFUSE_CHAR "<"
    !insertmacro DD_REFUSE_CHAR ">"
    !insertmacro DD_REFUSE_CHAR "|"
    !insertmacro DD_REFUSE_CHAR "/"
    !insertmacro DD_REFUSE_CHAR '"'
    ${StrContains} $R1 "\.." "$R0"
    ${If} $R1 != ""
      !insertmacro DD_REFUSE "Enter a full path, such as D:\Apps."
    ${EndIf}
    StrCpy $R1 $R0 "" 2
    ${StrContains} $R2 ":" "$R1"
    ${If} $R2 != ""
      !insertmacro DD_REFUSE "A folder name cannot contain any of these: ? * < > | / : $\""
    ${EndIf}

    ; Everything goes in one folder of its own, unless the pick already is one.
    StrLen $R3 $R0
    lastpart:
      ${If} $R3 <= 0
        StrCpy $R4 ""
        Goto gotpart
      ${EndIf}
      IntOp $R3 $R3 - 1
      StrCpy $R5 $R0 1 $R3
      StrCmp $R5 "\" 0 lastpart
      IntOp $R3 $R3 + 1
      StrCpy $R4 $R0 "" $R3
    gotpart:
    ${If} $R4 == "${DD_FOLDER}"
    ${OrIf} $R4 == "Relay"
      StrCpy $dd_Final $R0
    ${Else}
      StrCpy $dd_Final "$R0\${DD_FOLDER}"
    ${EndIf}

    StrLen $R1 $dd_Final
    ${If} $R1 > ${DD_MAX_PATH}
      !insertmacro DD_REFUSE "This path is too long for the phone's files. Choose a shorter one."
    ${EndIf}

    ; Uninstalling clears the program folder, so the data must not share it. (The installer adds the program's own
    ; folder name after this page: instFilesPre in electron-builder's assistedInstaller.nsh.)
    StrCpy $R6 $INSTDIR
    ${StrContains} $R1 "${APP_FILENAME}" "$INSTDIR"
    ${If} $R1 == ""
      StrCpy $R6 "$INSTDIR\${APP_FILENAME}"
    ${EndIf}
    StrLen $R7 $R6
    StrCpy $R8 $dd_Final $R7
    StrCpy $R9 $dd_Final 1 $R7
    ${If} $R8 == $R6
      ${If} $R9 == ""
      ${OrIf} $R9 == "\"
        !insertmacro DD_REFUSE "This is the program's folder, which uninstalling clears. Choose another one."
      ${EndIf}
    ${EndIf}
    StrLen $R7 $dd_Final
    StrCpy $R8 $R6 $R7
    StrCpy $R9 $R6 1 $R7
    ${If} $R8 == $dd_Final
    ${AndIf} $R9 == "\"
      !insertmacro DD_REFUSE "The program is being installed inside this folder. Choose another one."
    ${EndIf}

    ; The drive.
    StrCpy $0 $dd_Final 3
    StrCpy $R5 $dd_Final 2
    System::Call 'kernel32::GetDriveTypeW(w r0) i .r1'
    ${If} $1 <= 1
      !insertmacro DD_REFUSE "Drive $R5 was not found."
    ${ElseIf} $1 == 5
      !insertmacro DD_REFUSE "Drive $R5 is a disc drive. Choose another one."
    ${ElseIf} $1 == 4
      !insertmacro DD_REFUSE "Network drives are too slow for the phone. Choose a drive in this computer."
    ${EndIf}
    StrCpy $6 $1

    System::Call 'kernel32::GetVolumeInformationW(w r0, i 0, i 0, i 0, i 0, i 0, w .r5, i 64) i .r1'
    ${If} $1 == 0
      StrCpy $5 ""
    ${EndIf}
    ${If} $5 == "FAT32"
    ${OrIf} $5 == "FAT"
      !insertmacro DD_REFUSE "Drive $R5 uses FAT32, which cannot hold files over 4 GB. The phone's files are larger."
    ${EndIf}

    ; The space.
    System::Call 'kernel32::GetDiskFreeSpaceExW(w r0, *l .r2, *l .r3, *l .r4) i .r1'
    ${If} $1 != 0
      System::Int64Op $2 / 1048576
      Pop $dd_FreeMB
    ${EndIf}
    IntOp $7 $dd_FreeMB / 1024

    ${If} ${FileExists} "$dd_Final\config.json"
      StrCpy $dd_Message "penrose data is already in this folder and will be used as it is. $7 GB free."
    ${ElseIf} $dd_FreeMB == ""
      StrCpy $dd_Level 1
      StrCpy $dd_Message "Could not read the free space on $R5. penrose needs about 12 GB."
    ${ElseIf} $dd_FreeMB < ${DD_NEED_MB}
      StrCpy $dd_Level 2
      StrCpy $dd_Message "Only $7 GB free on $R5. penrose needs about 12 GB, so setup may not finish. Choose another drive or free some space."
    ${ElseIf} $dd_FreeMB < ${DD_ROOMY_MB}
      StrCpy $dd_Level 1
      StrCpy $dd_Message "$7 GB free on $R5. Enough to start, but tight: the phone grows as you add apps."
    ${ElseIf} $dd_FreeMB < ${DD_LIMIT_MB}
      StrCpy $dd_Message "$7 GB free on $R5. Enough for typical use."
    ${Else}
      StrCpy $dd_Message "$7 GB free on $R5. Enough even if the phone reaches its 64 GB limit."
    ${EndIf}

    ${If} $6 == 2
      !insertmacro DD_CAUTION "This is a removable drive: penrose cannot start while it is unplugged."
    ${EndIf}
    ${If} $5 != ""
    ${AndIf} $5 != "NTFS"
    ${AndIf} $5 != "ReFS"
      !insertmacro DD_CAUTION "This drive uses $5. NTFS is recommended for the phone."
    ${EndIf}

    ; The Android emulator can fail on paths with non-English letters.
    System::Call '*(&w${NSIS_MAX_STRLEN} "$dd_Final") i .r8'
    StrCpy $9 $8
    letters:
      System::Call '*$9(&i2 .r3)'
      ${If} $3 == 0
        Goto lettersdone
      ${EndIf}
      ${If} $3 > 127
      ${OrIf} $3 < 0
        !insertmacro DD_CAUTION "This path has non-English letters, which the Android emulator can fail on."
        Goto lettersdone
      ${EndIf}
      IntOp $9 $9 + 2
      Goto letters
    lettersdone:
    System::Free $8
  FunctionEnd

  ; Runs while installing: keeps the choice from the page, or from /DATADIR on a silent install.
  Function DataDirSave
    ${If} $dd_Final == ""
    ${AndIfNot} ${FileExists} "${DD_POINTER}"
      ClearErrors
      ${GetParameters} $0
      ${GetOptions} $0 "/DATADIR=" $dd_Parent
      ${IfNot} ${Errors}
        Call DataDirEvaluate
        ${If} $dd_Level == 3
          DetailPrint "Data folder not used: $dd_Message"
          StrCpy $dd_Final ""
        ${EndIf}
      ${EndIf}
    ${EndIf}
    ${If} $dd_Final != ""
      CreateDirectory "$dd_Final"
      CreateDirectory "${DD_POINTER_DIR}"
      FileOpen $0 "${DD_POINTER}" w
      FileWriteUTF16LE $0 "$dd_Final"
      FileClose $0
    ${EndIf}
  FunctionEnd
!macroend

!macro customPageAfterChangeDir
  !insertmacro DataDirLogic

  Page custom DataDirPageShow DataDirPageLeave

  Function DataDirRefresh
    ${NSD_GetText} $dd_Edit $dd_Parent
    Call DataDirEvaluate
    ${If} $dd_Final != ""
      ${NSD_SetText} $dd_FinalLabel "Data folder: $dd_Final"
    ${Else}
      ${NSD_SetText} $dd_FinalLabel ""
    ${EndIf}
    ${If} $dd_Stale != ""
      ${NSD_SetText} $dd_Status "$dd_Message$\r$\nYour earlier folder, $dd_Stale, was not found. If its drive is unplugged, cancel and plug it in first."
    ${Else}
      ${NSD_SetText} $dd_Status "$dd_Message"
    ${EndIf}
    ${If} $dd_Level == 0
      SetCtlColors $dd_Status 0x2E7D32 transparent
    ${ElseIf} $dd_Level == 1
      SetCtlColors $dd_Status 0x9A5B00 transparent
    ${Else}
      SetCtlColors $dd_Status 0xC62828 transparent
    ${EndIf}
    ; A colour change only shows once the label repaints.
    ShowWindow $dd_Status ${SW_HIDE}
    ShowWindow $dd_Status ${SW_SHOW}
    GetDlgItem $0 $HWNDPARENT 1
    ${If} $dd_Level == 3
      EnableWindow $0 0
    ${Else}
      EnableWindow $0 1
    ${EndIf}
  FunctionEnd

  Function DataDirChanged
    Pop $0
    Call DataDirRefresh
  FunctionEnd

  Function DataDirBrowse
    Pop $0
    ${NSD_GetText} $dd_Edit $1
    nsDialogs::SelectFolderDialog "Choose where penrose keeps its data" "$1"
    Pop $0
    ${If} $0 != "error"
    ${AndIf} $0 != ""
      ${NSD_SetText} $dd_Edit "$0"
      Call DataDirRefresh
    ${EndIf}
  FunctionEnd

  Function DataDirPageShow
    ${If} ${isUpdated}
      Abort
    ${EndIf}
    ; A folder chosen before stays, and so does the data of an install from before this page existed.
    StrCpy $dd_Stale ""
    ${If} ${FileExists} "${DD_POINTER}"
      FileOpen $0 "${DD_POINTER}" r
      FileReadUTF16LE $0 $1
      FileClose $0
      ${If} ${FileExists} "$1\*.*"
        Abort
      ${EndIf}
      StrCpy $dd_Stale $1
    ${ElseIf} ${FileExists} "$LOCALAPPDATA\Relay\config.json"
    ${OrIf} ${FileExists} "$LOCALAPPDATA\Relay\android\*.*"
      Abort
    ${EndIf}

    ${If} $dd_Parent == ""
      StrCpy $dd_Parent $LOCALAPPDATA
    ${EndIf}

    !insertmacro MUI_HEADER_TEXT "Choose where penrose keeps its data" "The phone and your files are stored here, apart from the program."
    nsDialogs::Create 1018
    Pop $0
    ${If} $0 == "error"
      Abort
    ${EndIf}

    ${NSD_CreateLabel} 0 0 100% 20u "Pick a drive with room. Most people use 10 to 12 GB, and the phone can grow to 64 GB at most. penrose makes its own folder inside the one you choose."
    Pop $0
    ${NSD_CreateText} 0 26u 78% 13u "$dd_Parent"
    Pop $dd_Edit
    ${NSD_OnChange} $dd_Edit DataDirChanged
    ${NSD_CreateButton} 80% 25u 20% 15u "Browse..."
    Pop $0
    ${NSD_OnClick} $0 DataDirBrowse
    ${NSD_CreateLabel} 0 46u 100% 12u ""
    Pop $dd_FinalLabel
    ${NSD_CreateLabel} 0 64u 100% 60u ""
    Pop $dd_Status

    Call DataDirRefresh
    nsDialogs::Show
  FunctionEnd

  Function DataDirPageLeave
    ${NSD_GetText} $dd_Edit $dd_Parent
    Call DataDirEvaluate
    ${If} $dd_Level == 3
      MessageBox MB_OK|MB_ICONSTOP "$dd_Message"
      Abort
    ${ElseIf} $dd_Level == 2
      MessageBox MB_YESNO|MB_ICONEXCLAMATION|MB_DEFBUTTON2 "$dd_Message$\r$\n$\r$\nUse this folder anyway?" IDYES useanyway
      Abort
      useanyway:
    ${EndIf}
    ; Can this folder be written to? Leaves nothing behind: the install makes it for real.
    ${IfNot} ${FileExists} "$dd_Final\*.*"
      CreateDirectory "$dd_Final"
      ${IfNot} ${FileExists} "$dd_Final\*.*"
        MessageBox MB_OK|MB_ICONSTOP "penrose cannot create $dd_Final. Choose another folder."
        Abort
      ${EndIf}
      RMDir "$dd_Final"
    ${EndIf}
  FunctionEnd
!macroend

!macro customInstall
  Push $0
  Call DataDirSave
  Pop $0
  ; Start penrose when the user logs in, which a reboot or a new boot comes to. A fresh install turns it on;
  ; an update leaves the user's choice alone. The value's name and its --hidden argument (start in the tray) are
  ; the ones desktop/src/autostart.js writes, so the app and the installer share one entry.
  ${IfNot} ${isUpdated}
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "penrose" '"$INSTDIR\${APP_EXECUTABLE_FILENAME}" --hidden'
    ; Task Manager's Startup apps: an earlier "disabled" from a previous install would keep it off.
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run" "penrose"
  ${EndIf}
!macroend

!macro customUnInstall
  ; An update runs the old uninstaller first; only a real uninstall takes the entry away.
  ${IfNot} ${isUpdated}
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "penrose"
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run" "penrose"
  ${EndIf}
!macroend
