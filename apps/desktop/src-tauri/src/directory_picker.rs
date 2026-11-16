// SPDX-License-Identifier: MPL-2.0
//! Native selection obtains a path; Python still validates every project operation.

use std::{path::Path, sync::{Mutex, MutexGuard}};

pub const UNAVAILABLE: &str = "The folder picker is unavailable. Enter a local directory manually.";
pub const FAILED: &str = "The folder picker could not select a directory. Try again or enter a path manually.";
const INVALID: &str = "Select an ordinary absolute local directory.";
static OPEN: Mutex<()> = Mutex::new(());

fn acquire() -> Result<MutexGuard<'static, ()>, &'static str> {
    OPEN.try_lock().map_err(|_| "A folder picker is already open.")
}

fn checked_path(path: String) -> Result<String, &'static str> {
    if !crate::workspace::valid_root(&path) {
        return Err(INVALID);
    }
    for directory in Path::new(&path).ancestors() {
        let metadata = std::fs::symlink_metadata(directory).map_err(|_| INVALID)?;
        if !metadata.is_dir() || metadata.file_type().is_symlink() {
            return Err(INVALID);
        }
        #[cfg(windows)]
        {
            use std::os::windows::fs::MetadataExt;
            const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
            if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
                return Err(INVALID);
            }
        }
    }
    Ok(path)
}

#[cfg(all(feature = "desktop", windows))]
pub fn select(owner: windows::Win32::Foundation::HWND) -> Result<Option<String>, &'static str> {
    use windows::{
        core::HRESULT,
        Win32::{
            Foundation::ERROR_CANCELLED,
            System::Com::{CoCreateInstance, CoInitializeEx, CoTaskMemFree, CoUninitialize,
                CLSCTX_INPROC_SERVER, COINIT_APARTMENTTHREADED, COINIT_DISABLE_OLE1DDE},
            UI::Shell::{FileOpenDialog, IFileOpenDialog, FOS_FORCEFILESYSTEM, FOS_NOCHANGEDIR,
                FOS_PATHMUSTEXIST, FOS_PICKFOLDERS, SIGDN_FILESYSPATH},
        },
    };
    struct Apartment;
    impl Drop for Apartment {
        fn drop(&mut self) { unsafe { CoUninitialize() }; }
    }
    let _open = acquire()?;
    // This function runs on a fresh STA thread; interfaces drop before its apartment.
    unsafe { CoInitializeEx(None, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE) }
        .ok().map_err(|_| FAILED)?;
    let _apartment = Apartment;
    let dialog: IFileOpenDialog = unsafe { CoCreateInstance(&FileOpenDialog, None, CLSCTX_INPROC_SERVER) }
        .map_err(|_| FAILED)?;
    let options = unsafe { dialog.GetOptions() }.map_err(|_| FAILED)?;
    unsafe { dialog.SetOptions(options | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST | FOS_NOCHANGEDIR) }
        .map_err(|_| FAILED)?;
    if let Err(error) = unsafe { dialog.Show(Some(owner)) } {
        return if error.code() == HRESULT::from_win32(ERROR_CANCELLED.0) {
            Ok(None)
        } else {
            Err(FAILED)
        };
    }
    let item = unsafe { dialog.GetResult() }.map_err(|_| FAILED)?;
    let raw = unsafe { item.GetDisplayName(SIGDN_FILESYSPATH) }.map_err(|_| FAILED)?;
    let selected = unsafe { raw.to_string() };
    unsafe { CoTaskMemFree(Some(raw.0.cast())) };
    checked_path(selected.map_err(|_| INVALID)?).map(Some)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_one_dialog_can_be_pending() {
        let first = acquire().unwrap();
        assert!(acquire().is_err());
        drop(first);
        assert!(acquire().is_ok());
    }

    #[test]
    fn selection_requires_an_existing_absolute_directory() {
        let base = std::env::temp_dir().join(format!("corinthos-picker-{}-{}", std::process::id(),
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()));
        std::fs::create_dir(&base).unwrap();
        let selected = base.join("Café folder");
        std::fs::create_dir(&selected).unwrap();
        assert_eq!(checked_path(selected.to_str().unwrap().to_owned()), Ok(selected.to_str().unwrap().to_owned()));
        let file = base.join("file.txt");
        std::fs::write(&file, b"owner bytes").unwrap();
        for value in ["relative".to_owned(), file.to_str().unwrap().to_owned(),
            base.join("missing").to_str().unwrap().to_owned(), "\0".to_owned()] {
            assert_eq!(checked_path(value), Err(INVALID));
        }
        #[cfg(windows)]
        for value in [r"\\host\share", r"\\?\C:\folder", "C:relative", "C:/folder:stream"] {
            assert_eq!(checked_path(value.to_owned()), Err(INVALID));
        }
        std::fs::remove_file(file).unwrap();
        std::fs::remove_dir(selected).unwrap();
        std::fs::remove_dir(base).unwrap();
    }
}
