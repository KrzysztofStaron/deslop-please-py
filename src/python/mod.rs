//! Python projects. `extract.py` reads the code with Python's own parser and
//! resolves names from imports, annotations, constructor calls and `self`;
//! this module turns its JSON into the model the lints read.

use std::collections::HashMap;
use std::path::Path;
use std::process::Command;

use serde_json::Value;

use crate::Error;
use crate::model::{
    File, FileId, Item, ItemId, ItemKind, Model, NodeId, NodeKind, Reference, Tree, Wrapper,
};

const EXTRACTOR: &str = include_str!("extract.py");

/// The model of the Python code under `root`.
pub fn build(root: &Path) -> Result<Model, Error> {
    let output = Command::new("python3")
        .args(["-I", "-c", EXTRACTOR])
        .arg(root)
        .output()
        .map_err(|e| Error::new(format!("cannot run python3: {e}")))?;
    if !output.status.success() {
        let message = String::from_utf8_lossy(&output.stderr);
        return Err(Error::new(format!("reading the code failed: {}", message.trim())));
    }
    let stderr = String::from_utf8_lossy(&output.stderr);
    if !stderr.trim().is_empty() {
        eprint!("{stderr}");
    }
    let json: Value = serde_json::from_slice(&output.stdout)
        .map_err(|e| Error::new(format!("the extractor's output is not JSON: {e}")))?;
    model_from(&json)
}

/// Whether `dir` holds Python code and no Cargo workspace.
pub fn is_python_project(dir: &Path) -> bool {
    ["pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"]
        .iter()
        .any(|name| dir.join(name).exists())
        || std::fs::read_dir(dir).is_ok_and(|entries| {
            entries
                .flatten()
                .any(|entry| entry.path().extension().is_some_and(|ext| ext == "py"))
        })
}

fn model_from(json: &Value) -> Result<Model, Error> {
    let mut tree = Tree::new();
    crate::model::use_dots();
    let mut nodes: Vec<NodeId> = Vec::new();
    for node in list(json, "nodes")? {
        let parent = match node["parent"].as_i64() {
            Some(index) if index >= 0 => nodes[index as usize],
            _ => tree.root(),
        };
        let kind = match text(node, "kind")? {
            "package" => NodeKind::Package,
            _ => NodeKind::Module,
        };
        let id = tree.child(parent, kind, text(node, "name")?);
        tree.node_mut(id).test = node["test"].as_bool().unwrap_or(false);
        nodes.push(id);
    }
    let files = list(json, "files")?
        .iter()
        .map(|file| {
            Ok(File {
                path: text(file, "path")?.to_owned(),
                module: nodes[index(file, "module")?],
                test: file["test"].as_bool().unwrap_or(false),
                lines: index(file, "lines")?,
            })
        })
        .collect::<Result<Vec<_>, Error>>()?;
    let items = list(json, "items")?
        .iter()
        .map(|item| {
            Ok(Item {
                name: text(item, "name")?.to_owned(),
                owner: nodes[index(item, "owner")?],
                module: None,
                file: Some(FileId::new(index(item, "file")?)),
                kind: match text(item, "kind")? {
                    "type" => ItemKind::Type,
                    "function" => ItemKind::Function,
                    "field" => ItemKind::Field,
                    "variant" => ItemKind::Variant,
                    "constant" => ItemKind::Constant,
                    _ => ItemKind::Other,
                },
                parent: item["parent"].as_u64().map(|p| ItemId::new(p as usize)),
                trait_impl: item["trait_impl"].as_bool().unwrap_or(false),
                takes_self: item["takes_self"].as_bool().unwrap_or(false),
                holds_nothing: false,
            })
        })
        .collect::<Result<Vec<_>, Error>>()?;
    let references = list(json, "references")?
        .iter()
        .map(|reference| {
            let file = index(reference, "file")?;
            Ok(Reference {
                file: FileId::new(file),
                line: index(reference, "line")? as u32,
                from: nodes[index(reference, "from")?],
                holder: reference["holder"].as_u64().map(|h| ItemId::new(h as usize)),
                item: ItemId::new(index(reference, "item")?),
                copies: reference["copies"].as_bool().unwrap_or(false),
                test: files[file].test,
            })
        })
        .collect::<Result<Vec<_>, Error>>()?;
    let mut wrappers = HashMap::new();
    for wrapper in list(json, "wrappers")? {
        let ids = |key: &str| -> Vec<ItemId> {
            wrapper[key]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_u64)
                .map(|id| ItemId::new(id as usize))
                .collect()
        };
        let value = match text(wrapper, "kind")? {
            "forwarder" => Wrapper::Forwarder {
                target: ItemId::new(index(wrapper, "target")?),
                fields: ids("fields"),
            },
            _ => Wrapper::Accessor {
                lent: ids("lent"),
                copied: ids("copied"),
                to_read: wrapper["to_read"].as_bool().unwrap_or(false),
            },
        };
        wrappers.insert(ItemId::new(index(wrapper, "item")?), value);
    }
    Ok(Model {
        tree,
        files,
        items,
        references,
        wrappers,
    })
}

fn text<'a>(value: &'a Value, key: &str) -> Result<&'a str, Error> {
    value[key].as_str().ok_or_else(|| missing(key))
}

fn index(value: &Value, key: &str) -> Result<usize, Error> {
    value[key]
        .as_u64()
        .map(|n| n as usize)
        .ok_or_else(|| missing(key))
}

fn list<'a>(value: &'a Value, key: &str) -> Result<&'a Vec<Value>, Error> {
    value[key].as_array().ok_or_else(|| missing(key))
}

fn missing(key: &str) -> Error {
    Error::new(format!("the extractor's output has no {key:?}"))
}
